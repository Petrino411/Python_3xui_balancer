# xray-client-balancer

Сервис sticky-распределения клиентов панели **3x-ui** по балансировщикам **Xray**.
Реализован по ТЗ `xray_client_balancer_TZ.md` (лежит рядом).

Двухуровневая схема:

```
уровень 1 (этот сервис):  client -> sticky assignment -> client-balancer-N
уровень 2 (сам Xray):     client-balancer-N -> primary, при падении -> fallback
```

Сервис **не** перераспределяет клиентов при каждом запуске: назначение хранится в
SQLite, привязка идёт по внутреннему id клиента панели (стабильному при смене email),
перераспределение — только при удалении клиента или явной команде `rebalance`.

---

## 1. Установка

Зависимости: Python 3.11+, `httpx`, `pydantic>=2`, `PyYAML`.

```bash
# вариант A: venv (нужен пакет python3-venv)
python3 -m venv /opt/xray-client-balancer/.venv
/opt/xray-client-balancer/.venv/bin/pip install -e /opt/xray-client-balancer

# вариант B: без venv (работает всегда, в т.ч. там, где нет ensurepip)
pip install --target /opt/xray-client-balancer/deps httpx pydantic PyYAML
# запуск тогда: PYTHONPATH=/opt/xray-client-balancer/deps:/opt/xray-client-balancer/src python3 -m xray_client_balancer.main ...
```

Код: `src/xray_client_balancer/`. CLI: `python3 -m xray_client_balancer.main <команда>`
(на узле удобнее обёртка `tools/xcb.sh` → `/usr/local/bin/xcb`, см. §2.1).

Конфиг: `/etc/xray-client-balancer/config.yaml` (образец — `config.example.yaml`).
Токен панели в файле не хранится, он берётся из окружения:

```bash
XRAY_BALANCER_API_TOKEN=$(/usr/local/x-ui/x-ui setting -getApiToken -tokenName cli-fallback | sed -n 's/^apiToken: //p')
```

systemd: `systemd/xray-client-balancer.service` (кладётся в `/etc/systemd/system/`,
в нём `EnvironmentFile=/etc/xray-client-balancer/env`). Юнит **не включается и не
запускается автоматически** — сначала `check-api` и `sync --dry-run`.

Развёртывание на боевом узле по шагам (каталоги, доставка кода, `webPort`/`webBasePath`,
ротация токена, конфиг, юнит, приёмка, откат, удаление) — §9.

---

## 2. Команды

| команда | что делает |
|---|---|
| `check-api` | диагностика API панели (8 проверок из ТЗ), ничего не меняет |
| `sync --dry-run` | показать, что сервис сделал бы: новые/удалённые клиенты, распределение, изменения routing. Ничего не пишет |
| `sync` | одна синхронизация |
| `sync --force-write` | обойти churn-предохранитель |
| `daemon` | цикл синхронизации (`sync.interval_seconds`, по умолчанию 30 с) |
| `status` | распределение из локального состояния + live-состояние балансировщиков из ядра |
| `list` | клиенты по группам |
| `clients` | клиенты с **id** и группой (что подавать в `move`); `--filter`, `--group`, `--offline` |
| `balancers` | группы сервиса: primary, strategy, число клиентов; `--live` — спросить ядро |
| `move` (`assign`) | **переназначить клиента(ов)** между балансировщиками; по умолчанию только план |
| `rebalance --yes` | принудительно выровнять всех клиентов заново |
| `doctor` | замеры памяти процесса и роста диска (state.db/WAL, бэкапы, свободное место) |

### 2.1 Переназначение клиента: `move`

Ниже и в остальном README `xcb` — это обёртка на узле (`tools/xcb.sh` →
`/usr/local/bin/xcb`), которая читает токен из `/etc/xray-client-balancer/env` и
запускает модуль. Локально то же самое: `python3 -m xray_client_balancer.main move ...`
или `xray-client-balancer move ...` из venv.

Первый запуск без `--yes` — всегда только план, ничего не меняется:

```bash
xcb clients                     # у кого какой id и группа
xcb move anna@example 2         # план: anna@example -> client-balancer-2
xcb move anna@example 2 --yes   # выполнить (2 = сокращение для client-balancer-2)
```

Что именно происходит при `--yes`: меняется одна строка в `state.db` (+ один `sync`),
из правил routing переписываются только те, где встречается этот клиент. Такие правки
(routing по `user`) панель применяет hot-apply — ядро **не** перезапускается; команда
печатает uptime ядра до/после, чтобы это было видно, а не на веру.

Остальные режимы:

```bash
xcb move --to client-balancer-1 anna@example bob@example --yes   # несколько клиентов, один sync
xcb move anna@example --auto --yes      # в самую свободную группу
xcb move --id 17 --to 3 --yes           # по внутреннему id (email поменяли — id стабилен)
xcb move bob                           # подстрока email, если она однозначна
xcb move --no-verify anna@example 2 --yes  # не спрашивать ядро про маршрут
```

Коды выхода: `0` — сделано/показан план, `1` — ошибка (панель недоступна или конфиг не
принят; подробности в выводе), `2` — отказ по существу: клиента нет, подстрока
неоднозначна, тега нет в конфиге, клиент исключён `exclude_clients`. Отказ (2) не пишет
ни в панель, ни в state.db **ничего**. При коде 1 состояние зависит от места сбоя: если
панель была недоступна на чтении — не изменено ничего; если конфиг не приняли после
записи назначений — назначение уже в `state.db`, и демон допишет конфиг на следующем
цикле (это же напечатает `sync`).
Найденный клиент проверяется по панели (существует ли он), поэтому клиент, которого в
панели нет, перенести нельзя — это опечатка, а не задача.

Обратный перенос — та же команда с прежним тегом; команда сама печатает готовую строку
(`xcb move <email> <прежний тег> --yes`).

`xcb rebalance --yes`, в отличие от `move`, раскладывает **всех** заново и отменяет
ручные переносы — не используйте его как «исправить один перекос».

Выход `sync` (§22):

```
Found clients: 31

New:
  anna@example -> client-balancer-2

Removed:
  old@example

Changes to routing:
  client-balancer-1: 10
  client-balancer-2: 11
  client-balancer-3: 10

Routing updated successfully
```

---

## 3. Что сервис меняет в конфиге — и что не меняет никогда

Своими сервис считает **только**:

1. `routing.rules` с `balancerTag` из своего конфига (и/или с комментарием `xcb-managed:<tag>`);
2. `routing.balancers` с тегами из своего конфига;
3. свои outbound-теги внутри `subjectSelector` секции healthcheck (`observatory` / `burstObservatory`).

Всё остальное — чужие правила, чужие балансировщики, `domainStrategy`, `log`, `dns`,
`policy`, inbound'ы, outbound'ы — не трогается. Проверки, которые это гарантируют:

* перед записью конфиг **перечитывается** и `foreign_signature` (отпечаток чужой части)
  сравнивается с тем, что было прочитано раньше: если админ правил конфиг в это время,
  цикл отменяется (защита от потерянного обновления);
* после записи чужой отпечаток сверяется снова; при расхождении выполняется один
  автоматический повторный merge (не откат), при повторе — CRITICAL и churn-предохранитель;
* сравнение правил — **мультимножеством**: панель при сохранении сама переставляет своё
  `api`-правило в начало и сортирует ключи, это не считается потерей;
* сервис никогда не удаляет секции healthcheck, которые не создавал сам.

---

## 4. Защиты (§10, §32, §33, §37)

* **Ядро не поднялось после записи** → конфиг автоматически возвращается к предыдущему
  (из бэкапа), событие пишется как CRITICAL. Ожидание подъёма — `safety.xray_ready_timeout`
  (30 с).
* **Панель вернула пустой список клиентов**, а назначения в состоянии есть → ничего не
  удаляется, синхронизация отклоняется (`safety.refuse_empty_panel`).
* **Ошибка API/панели** → состояние и routing остаются как были, синхронизация
  повторяется на следующем цикле.
* **Структурная валидация кандидата до записи**: существуют ли outbound-теги (в шаблоне
  или в работающем конфиге), ровно один `primary` в `selector`, нет пустых списков
  `user`, нет дубликатов клиента между группами.
* **Churn-предохранитель**: если конфиг приходится «починять» `safety.churn_breaker_cycles`
  циклов подряд, автоматические изменения останавливаются до `sync --force-write`.
* **Бэкапы**: перед каждой записью шаблон сохраняется в `backups.directory`
  (хранится `backups.keep` последних). Это же — материал для отката.

Откат руками: взять нужный файл из `backups.directory` и записать его через панель
(`sync` сам вернётся к нему при следующем запуске, если routing разошёлся).

---

## 5. Измерения на живых панелях 3x-ui 3.8.5 / Xray 26.9.x

Всё ниже проверено на реальных серверах, а не взято из документации:

| факт | следствие для сервиса |
|---|---|
| `POST /panel/api/xray/` возвращает `obj` **JSON-строкой** вида `{"xraySetting": {...}}` | парсер поддерживает и строку, и объект |
| панель перезапускает ядро при **каждом** принятом `update`, даже если содержимое не изменилось | запись только когда конфиг реально расходится (`routing.template_changed`) |
| панель проверяет только синтаксис JSON и запуск ядра: конфиг без outbounds она **примет** | структурную валидацию делает сам сервис, а не панель |
| при неудачном старте панель ретраит ядро каждые 2 с, `routeTest` отвечает `xray is not running` | после записи сервис ждёт готовности ядра, а не считает это ошибкой маршрутизации |
| `server/status.xray` = `{state, errorMsg, version}`, `appStats.uptime` — секунды работы ядра | детектор перезапуска и условие автоотката |
| `routeTest` без `inboundTag` даёт `matched=false`, если правило матчится по inbound | проверка маршрутов перебирает теги inbound'ов из работающего конфига |
| outbound'ы могут приходить не из шаблона, а из подписок панели (`sub*-tls-*`) | валидация знает теги и из работающего конфига (`server/getConfigJson`) |
| `balancerStatus` возвращает `selected`/`override`/`running` на живых балансировщиках | используется в `status` |
| изменения только в `routing` панель применяет **без рестарта** ядра (hot-apply); правка `observatory`/`burstObservatory` — только с рестартом | на узле, где healthcheck-секция уже покрывает наши теги, первая запись проходит без разрыва соединений |

Стратегии Xray (`infra/conf/router.go`) приводятся к нижнему регистру, поэтому
`leastPing`/`leastLoad`/`random`/`roundRobin` пишутся как в ТЗ; для `leastLoad` можно
передавать тонкие настройки через `strategy_settings` (baselines/expected/tolerance/maxRTT).

---

## 5.1 Память и диск: чем это проверяется

Демон работает месяцами, поэтому «не течёт по памяти» и «не забьёт диск» — это
утверждения, которые нужно периодически подтверждать замерами:

```bash
xcb doctor                                    # свой процесс + файлы + свободное место
xcb doctor --pid $(systemctl show -p MainPID --value xray-client-balancer)
```

`doctor` ничего не меняет (все проверки read-only, БД открывается через `mode=ro`) и
печатает `OK/WARN` по каждому пункту: RSS и дескрипторы процесса, размер state.db и
WAL, число/размер бэкапов, свободное место на обеих файловых системах, мусорные файлы
рядом с БД. Пороги — в `src/xray_client_balancer/health.py`. Выход 1 при любом WARN,
так что команду можно ставить в cron/мониторинг.

Что ограничивает рост на диске (всё — по коду, проверено замерами ниже):

| файл/каталог | почему не растёт бесконечно |
|---|---|
| `state.db` | строк столько же, сколько клиентов (плюс ~10 строк `meta`); удалённые клиенты удаляются из таблицы |
| `state.db-wal` | SQLite сам делает checkpoint (1000 страниц ≈ 4 МБ) и переиспользует журнал; порог WARN в doctor — 32 МБ, он срабатывает, если checkpoint перестал проходить |
| `backups/` | ротация по `backups.keep` (20 по умолчанию) — файлы старее удаляются при каждой записи |
| `/tmp` | `xray -test` пишет кандидата в `tempfile` и удаляет сразу после проверки |
| journal | фиксированное число строк на цикл + одна строка ресурсов в час (см. ниже) |

Сам демон раз в час пишет в journal строку вида
`Ресурсы: rss=45.4 MiB fds=10 state=36.0 KiB wal=616.0 KiB backups=… fs_free=…` —
по ней рост видно в `journalctl -u xray-client-balancer` без запуска чего-либо ещё.

### Замеры (`tools/soak_memory.py`)

Прогон циклов синхронизации против mock-панели (боевая не затрагивается), перед
каждым замером вызывается `gc.collect()` — тогда рост числа объектов означает
достижимые объекты (утечку), а не ещё не собранный мусор:

```
# устойчивый режим: 150 клиентов, панель не меняется
python3 tools/soak_memory.py cycles --cycles 6000 --clients 150 --no-churn --tick 500
# режим с изменениями: клиентов добавляют/удаляют/переименовывают/переносят
python3 tools/soak_memory.py cycles --cycles 3000 --clients 60 --tick 500
# настоящий процесс daemon под наблюдением (снимается /proc/<pid>)
python3 tools/soak_memory.py daemon --seconds 180
```

Результаты на этом узле (Python 3.11.16, 16 ядер, mock-панель на loopback):

| прогон | что показал |
|---|---|
| `cycles --cycles 6000 --clients 150 --no-churn` | RSS 42.1 → 42.6 MiB за 6000 циклов, причём весь рост пришёлся на первые 500 циклов (прогрев), дальше ровно 42.6 MiB; объектов GC 35 139 → 35 046 (−93); state.db 4.0 KiB (7 страниц); дескрипторов 11; 0 ошибок циклов |
| `cycles --cycles 3000 --clients 60` (клиентов добавляют, удаляют, переименовывают и переносят) | RSS 42.0 → 43.0 MiB; объектов GC 34 869 → 35 090 (число клиентов при этом выросло с 60 до 160); state.db 32 KiB (8 страниц); бэкапов **ровно 20** на 155.8 KiB при `keep=20`; 161 запись конфига, 0 ошибок |
| `daemon --seconds 180 --clients 150` (настоящий процесс, снимается `/proc/<pid>`) | RSS 40.0 → 40.3 MiB; дескрипторов 7 и не меняется; журнал — 3 строки (215 байт) на цикл, то есть ~0.6 МиБ в сутки при интервале 30 с |
| `daemon --seconds 600 --clients 150` (то же, 10 минут; в стенде интервал 5 с вместо 30) | RSS 40.0 MiB на 60-й секунде → 40.1 MiB на 600-й (+0.1 MiB); дескрипторов ровно 7 на всех 11 замерах; 0 ошибок циклов |
| `tests/test_disk_growth.py` (3000 транзакций записи, как в установившемся режиме) | state.db выходит на 28 672 B (7 страниц) уже к 1000-й транзакции и дальше не растёт; WAL доходит до 4 120 032 B (≈3.93 МиБ — это порог checkpoint в 1000 страниц) и **замирает**: с 1000-й по 3000-ю транзакцию размер не меняется; после закрытия последнего соединения WAL усечён до 0 |

Итого по обоим вопросам: память после прогрева не растёт (плато, объекты GC не
накапливаются), диск ограничен — state.db десятки килобайт, WAL ~4 МиБ с
переиспользованием, бэкапы ≤ `keep` × размер шаблона, журнал ~0.6 МиБ в сутки.
Если `doctor` когда-нибудь покажет WAL больше 32 МиБ — значит checkpoint перестал
проходить (долгая транзакция или второй процесс на той же БД), и это уже сигнал.

---

## 6. Тесты

```bash
python3 -m pytest -q          # 114 тестов, включая интеграционный сценарий §42
```

`tools/soak_memory.py` — отдельный замер устойчивости (не тест): гоняет циклы
синхронизации против mock-панели и печатает RSS, объекты GC, размер state.db/WAL и
бэкапов; либо наблюдает за настоящим процессом `daemon`. Подробности и цифры — §5.1.

`tests/mock_panel.py` — mock панели 3x-ui на настоящем HTTP: авторизация Bearer,
404 без токена, `obj` строкой, нормализация шаблона при сохранении, состояние ядра,
`routeTest` по правилам конфига. Тесты покрывают: распределение 3/4/10/150 клиентов,
удаление, детерминизм, смену email у клиента, клиента в нескольких inbound,
отсутствие перераспределения при удалении, отказ при пустом ответе панели, отказ при
недоступном API, идемпотентность (повторный `sync` не пишет конфиг и не меняет
временные метки), автооткат при неподнявшемся ядре, routeTest с inbound-тегами.

Отдельно покрыты новые команды и замеры:

* `tests/test_move.py` — точечный перенос: план ничего не пишет, `--yes` правит только
  указанных клиентов и делает ровно одну запись конфига, перенос по id/подстроке,
  `--auto` раскладывает нескольких клиентов в разные группы, отказы (нет клиента,
  неоднозначная подстрока, нет тега, клиент исключён) — код 2 и ни одной записи,
  недоступная панель — код 1;
* `tests/test_doctor.py` — `doctor` показывает настоящие размеры файлов, ловит
  разросшийся WAL и незакрытые `xcb-candidate-*`, не ругается на чужие файлы рядом с БД;
* `tests/test_disk_growth.py` — на 3000 транзакциях записи state.db выходит на 7 страниц,
  WAL — на 4 МиБ и дальше не растёт (плато checkpoint'а SQLite), таблица `meta` не растёт.

---

## 7. Конфиг: полный пример

```yaml
panel:
  url: "https://127.0.0.1:21868/<base-path>/"   # base path обязателен, если включён
  api_token: "${XRAY_BALANCER_API_TOKEN}"
  verify_tls: true
  ca_bundle: "/root/cert/ip/fullchain.pem"
  tls_verify_hostname: false      # панель слушает loopback, сертификат на другое имя
  timeout_seconds: 15
  retries: 3
  backoff_seconds: 3
  max_backoff_seconds: 15

balancers:
  - tag: "client-balancer-1"
    primary: "sub1-mskserv"
    strategy: "leastLoad"
    strategy_settings: { expected: 1, baselines: ["300ms"], maxRTT: "1s", tolerance: 0.1 }
  - tag: "client-balancer-2"
    primary: "sub2-cringe"
    strategy: "leastLoad"
  - tag: "client-balancer-3"
    primary: "sub3-tls-riga2-tls"
    strategy: "leastLoad"

fallback:
  outbound: "sub1-tls-stock-tls"

routing:
  managed_position: "bottom"      # bottom | top | after_rule
  insert_after_rule: null
  write_rule_tag: true            # писать comment xcb-managed:<tag> для наглядности
  observatory:
    type: "auto"                  # auto | observatory | burst | none
    probe_url: "https://www.gstatic.com/generate_204"
    probe_interval: "30s"

sync:
  interval_seconds: 30

state:
  database: "/var/lib/xray-client-balancer/state.db"

backups:
  enabled: true
  directory: "/var/lib/xray-client-balancer/backups"
  keep: 20

safety:
  refuse_empty_panel: true
  config_repair_attempts: 1
  churn_breaker_cycles: 3
  xray_ready_timeout: 30

validation:
  local_xray_test: false          # true = прогонять `xray run -test` локально до записи
  xray_binary: "/usr/local/x-ui/bin/xray-linux-amd64"
  route_test: true                # проверять маршрут через routeTest после записи
  route_test_domain: "example.com"

exclude_clients: []               # email'ы, которые сервис не трогает
exclude_regex: []
include_disabled: true            # §27: выключенные клиенты остаются в правилах
```

---

## 8. Порядок первого запуска (кратко)

Полная пошаговая инструкция развёртывания на сервере — в §9. Здесь только суть.

```bash
# 1. диагностика API (ничего не пишет)
python3 -m xray_client_balancer.main check-api --config /etc/xray-client-balancer/config.yaml

# 2. что сервис сделал бы
python3 -m xray_client_balancer.main sync --dry-run --config /etc/xray-client-balancer/config.yaml

# 3. одна реальная синхронизация (создаст бэкап перед записью)
python3 -m xray_client_balancer.main sync --config /etc/xray-client-balancer/config.yaml

# 4. состояние
python3 -m xray_client_balancer.main status --config /etc/xray-client-balancer/config.yaml

# 5. демон
systemctl enable --now xray-client-balancer

# 6. замеры памяти и диска (в том числе у самого демона)
xcb doctor --pid $(systemctl show -p MainPID --value xray-client-balancer)
```

Если `sync` отказывается писать и пишет `outbound 'X' отсутствует и в шаблоне, и в
работающем конфиге` — значит в панели ещё нет outbound'ов с такими тегами: создайте их
(или поправьте `balancers`/`fallback` в конфиге). Это защита, а не ошибка.

---

## 9. Развёртывание на сервере вручную (пошагово)

Инструкция предполагает, что «сервер» — это узел, на котором стоит панель **3x-ui**
(она же управляет ядром Xray) и куда клиенты приходят, а на «рабочей машине» лежит этот
каталог с кодом. Всё делается руками: ни одного шага, который сервис выполняет сам, тут
нет — наоборот, каждый шаг можно проверить и откатить.

Обозначения: `HOST` — адрес узла (`root@185.117.155.8`), `HERE` — этот каталог на рабочей
машине (`/home/petrino/Documents/Python_3xui_balancer`). Команды с `root@HOST` выполняются
на узле, команды без — на рабочей машине.

Порядок шагов важен: сначала предусловия и бэкап, потом код, потом конфиг, потом чтение
(без записи), и только в конце — `sync` и запуск демона.

### 9.1 Предусловия (проверить до всего остального)

```bash
# на узле
python3 -V                          # нужен >= 3.11 (на 3.10 сервис не запустится)
systemctl is-active x-ui            # active
/usr/local/x-ui/bin/xray-linux-amd64 version | head -3
ls -l /usr/local/x-ui/bin/config.json
df -h /                             # свободно хотя бы 200 МБ: код+deps ~10 МБ, WAL ~4 МБ, бэкапы keep x размер шаблона
ss -tlnp | grep -E 'x-ui|xray'      # ядро и панель слушают loopback
```

Что нужно знать заранее:

* **webPort и webBasePath панели** — из них собирается `panel.url` (см. §9.5).
* **outbound-теги** для балансировщиков и резерва. Они должны существовать в панели
  (в шаблоне или в подписках, `outbound_subscriptions`). Если тега нет нигде, `sync`
  откажется писать routing — это защита §10, а не поломка.
* **имя API-токена**, которым будет пользоваться сервис. Он берётся из таблицы
  `api_tokens` панели (см. §9.5). Каждый вызов `-getApiToken` **ротирует** токен, поэтому
  сервису стоит завести **отдельное имя** — тогда ротация не тронет токены других
  потребителей (скриптов, других узлов).

Требования по версиям: сервис написан под Python 3.11+; панель 3.8.x (на ней всё измерено и
проверено); Xray 26.9.x. На панели 3.6.x другой формат ответов API — сервис её не поддерживает.

### 9.2 Бэкап перед началом (обязательно)

```bash
# на узле
TS=$(date +%F-%H%M%S); mkdir -p /root/xui-backup-$TS && cp -a /etc/x-ui/x-ui.db* /root/xui-backup-$TS/
echo "бэкап: /root/xui-backup-$TS"
```

Сервис трогает только секцию `routing` шаблона и никогда — inbounds/outbounds/клиентов, но
бэкап БД панели (вместе с `-wal`/`-shm`!) стоит один `cp` и снимает весь класс «а если».
Путь бэкапа запишите в журнал изменений узла.

### 9.3 Каталоги

```bash
# на узле
install -d -m 0755 /opt/xray-client-balancer
install -d -m 0700 /etc/xray-client-balancer
install -d -m 0700 /var/lib/xray-client-balancer /var/lib/xray-client-balancer/backups
```

`/etc/xray-client-balancer` — 0700, потому что внутри лежит env с токеном панели.

### 9.4 Доставка кода на узел

Вариант А (rsync с рабочей машины; самый удобный для последующих обновлений):

```bash
# с рабочей машины
rsync -av \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.egg-info' --exclude '.git' \
  HERE/ root@HOST:/opt/xray-client-balancer/
```

Если узел уже прошит руками (например в `/opt/xray-client-balancer/tools/ops/` лежит
что-то, чего нет в этом каталоге), **не** добавляйте `--delete`: он снесёт локальные файлы
узла. Сверьтесь один раз (`ls -R /opt/xray-client-balancer/tools`) и решите, что из этого
нужно сохранить.

Вариант Б (если rsync на узле нет — tar по ssh):

```bash
# с рабочей машины
cd HERE && tar --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude='*.egg-info' -czf /tmp/xcb-src.tgz .
ssh root@HOST 'mkdir -p /opt/xray-client-balancer'
ssh root@HOST 'tar -xzf - -C /opt/xray-client-balancer' < /tmp/xcb-src.tgz
```

Проверка после копирования:

```bash
# на узле
ls /opt/xray-client-balancer        # README.md pyproject.toml src systemd tests tools
ls /opt/xray-client-balancer/src/xray_client_balancer/   # 12 .py файлов
```

### 9.5 webPort, webBasePath, outbound-теги и токен

Всё это читается из БД панели read-only (`sqlite3` на узле обычно нет, есть python3):

```bash
# на узле: порт, base path, сертификаты панели
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("file:/etc/x-ui/x-ui.db?mode=ro", uri=True)
for key in ("webPort", "webBasePath", "webCertFile", "webKeyFile"):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    print(f"{key:12} = {row[0] if row else '(нет)'}")
for name, enabled, scope in db.execute("SELECT name, enabled, scope FROM api_tokens"):
    print(f"api_token    = {name} (enabled={enabled}, scope={scope})")
PY
```

```bash
# на узле: какие outbound-теги реально есть у работающего ядра (шаблон + подписки)
python3 - <<'PY'
import json, sqlite3
db = sqlite3.connect("file:/etc/x-ui/x-ui.db?mode=ro", uri=True)
row = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
tpl = json.loads(row[0]) if row and row[0] else {}
print("в шаблоне :", sorted(str(o.get("tag")) for o in tpl.get("outbounds") or []))
live = json.load(open("/usr/local/x-ui/bin/config.json"))
print("работает  :", sorted(str(o.get("tag")) for o in live.get("outbounds") or []))
PY
```

Первые колонки — это теги для `balancers[].primary` и `fallback.outbound`. Ориентируйтесь
на вторую строку: в 3.8.x outbound'ы могут приходить из подписок и в шаблоне их не будет.

Токен (важно про ротацию):

```bash
# на узле: получить (и тем самым СРОТИРОВАТЬ) токен указанного имени
/usr/local/x-ui/x-ui setting -getApiToken -tokenName xcb
# вывод: apiToken: <длинная строка>  — сохранить её сразу, второй раз её не покажут
```

Что нужно знать про этот вызов (проверено на 3.8.5):

* в БД лежит только SHA-256, plaintext показывается **один раз** — сразу кладите его в env;
* каждый вызов инвалидирует **предыдущий токен того же имени**, поэтому:
  1) заведите отдельное имя (`xcb`) для сервиса;
  2) после любой ротации сразу обновите `/etc/xray-client-balancer/env` и `systemctl restart
     xray-client-balancer`, иначе сервис получит 401;
* без токена API отвечает **404** (не 401) с пустым телом — так панель скрывает эндпоинты,
  поэтому «404 на /panel/api/*» ≠ «эндпоинта нет».

Проверить, что токен рабочий, **до** настройки сервиса:

```bash
# на узле
PORT=21868; BASE=/AbCdEf1234; TOKEN=<токен выше>
curl -s -o /dev/null -w 'без токена: %{http_code}\n' "https://127.0.0.1:$PORT$BASE/panel/api/server/status" -k
curl -s -o /dev/null -w 'с токеном:  %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
     "https://127.0.0.1:$PORT$BASE/panel/api/server/status" -k
# ожидаем: без токена 404, с токеном 200
```

### 9.6 Зависимости Python на узле

Вариант А (venv; если есть `python3-venv`):

```bash
# на узле
python3 -m venv /opt/xray-client-balancer/.venv
/opt/xray-client-balancer/.venv/bin/pip install -e /opt/xray-client-balancer
/opt/xray-client-balancer/.venv/bin/xray-client-balancer --version    # должно напечатать версию
```

Вариант Б (venv недоступен — часто на минимальных образах нет `ensurepip`):

```bash
# на узле
python3 -m pip install --break-system-packages --target /opt/xray-client-balancer/deps httpx pydantic PyYAML
PYTHONPATH=/opt/xray-client-balancer/src:/opt/xray-client-balancer/deps \
  python3 -m xray_client_balancer.main --version
```

Запомните, какой вариант сработал: от него зависит строка `ExecStart` в юните (§9.10) и
то, нужна ли обёртка `xcb` (§9.8).

Если запускаете модуль напрямую (без обёртки `xcb`), токен надо положить в окружение руками:

```bash
set -a; . /etc/xray-client-balancer/env; set +a     # после этого ${XRAY_BALANCER_API_TOKEN} подставится
```

### 9.7 Конфиг и env

Секрет в файле конфига не хранится — только имя переменной окружения.

```bash
# на узле
install -m 0600 /dev/null /etc/xray-client-balancer/env
cat > /etc/xray-client-balancer/env <<'EOF'
XRAY_BALANCER_API_TOKEN=<токен из 9.5>
EOF
chmod 600 /etc/xray-client-balancer/env
```

```bash
# на узле: конфиг (пример под панель с base path и сертификатом на публичное имя)
cat > /etc/xray-client-balancer/config.yaml <<'EOF'
panel:
  url: "https://127.0.0.1:21868/AbCdEf1234/"   # webPort + webBasePath из 9.5, слеш в конце
  api_token: "${XRAY_BALANCER_API_TOKEN}"
  verify_tls: true
  ca_bundle: "/root/cert/ip/fullchain.pem"     # сертификат панели; если самоподписанный
  tls_verify_hostname: false                   # панель слушает loopback с чужим именем в сертификате
  timeout_seconds: 15
  retries: 4                                   # 5s -> 10s -> 20s -> 30s

balancers:
  - tag: "client-balancer-1"                   # теги ниже — из 9.5, менять на свои
    primary: "sub1-mskserv"
    strategy: "leastLoad"
    strategy_settings: { expected: 1, baselines: ["300ms"], maxRTT: "1s", tolerance: 0.1 }
  - tag: "client-balancer-2"
    primary: "sub2-cringe"
    strategy: "leastLoad"
  - tag: "client-balancer-3"
    primary: "sub3-tls-riga2-tls"
    strategy: "leastLoad"

fallback:
  outbound: "sub1-tls-stock-tls"               # резерв на случай падения primary

routing:
  managed_position: "bottom"                   # свои правила — ниже пользовательских
  write_rule_tag: true

sync:
  interval_seconds: 30

state:
  database: "/var/lib/xray-client-balancer/state.db"

backups:
  enabled: true
  directory: "/var/lib/xray-client-balancer/backups"
  keep: 20

safety:
  refuse_empty_panel: true
  config_repair_attempts: 1
  churn_breaker_cycles: 3
  xray_ready_timeout: 30

validation:
  local_xray_test: true                        # сервис стоит на узле с панелью — пусть проверяет ядром
  xray_binary: "/usr/local/x-ui/bin/xray-linux-amd64"
  route_test: true
  route_test_domain: "example.com"

exclude_clients: []                            # email'ы, которых сервис не трогает
include_disabled: true
EOF
chmod 600 /etc/xray-client-balancer/config.yaml
```

Ориентир по `panel.url`: он должен совпадать с тем, что вы проверяли curl'ом в §9.5 —
включая base path и завершающий слеш. Если `webBasePath` пустой, URL — `https://127.0.0.1:<webPort>/`.

### 9.8 Обёртка `xcb` (не обязательно, но удобно)

`xcb` — это `/usr/local/bin/xcb` из `tools/xcb.sh`: он читает токен из env-файла, собирает
`PYTHONPATH` и вызывает модуль. Токен при этом не попадает ни в командную строку, ни в
историю шелла, ни в `ps`.

```bash
# на узле
install -m 0755 /opt/xray-client-balancer/tools/xcb.sh /usr/local/bin/xcb
xcb --version
xcb --config /etc/xray-client-balancer/config.yaml balancers
```

Дальше в инструкции команды записаны как `xcb ...`; без обёртки используйте
`/opt/xray-client-balancer/.venv/bin/xray-client-balancer ...` (вариант А из §9.6) или
`PYTHONPATH=/opt/xray-client-balancer/src:/opt/xray-client-balancer/deps python3 -m
xray_client_balancer.main ...` (вариант Б).

### 9.9 Проверки без записи (обязательные)

```bash
# на узле
xcb check-api     --config /etc/xray-client-balancer/config.yaml    # проверки API, ничего не меняет
xcb sync --dry-run --config /etc/xray-client-balancer/config.yaml   # что сервис сделал бы
```

Как читать результат:

* `check-api` должен показать `ИТОГО: все N проверок пройдены`. Провал `authentication` —
  токен/base path; провал `read current Xray config` — панель отвечает не тем, что ждём.
  У `check-api` есть ключ `--allow-write`: он дополнительно отправляет в панель **идентичный**
  конфиг, а панель перезапускает ядро на каждом принятом `update` — то есть ключ даёт
  короткий разрыв. Пользуйтесь им осознанно (или не пользуйтесь: проверка не обязательна).
* В `sync --dry-run` смотрим: `Found clients: N`, блок `New:` (кого сервис назначит),
  `Changes to routing: client-balancer-1: X ...` и/или ошибки вида
  `outbound 'X' отсутствует и в шаблоне, и в работающем конфиге`.
  Ошибка про outbound — единственная частая причина остановки: создайте outbound/подписку
  в панели или поправьте теги в `config.yaml`.
* `--dry-run` **не пишет ничего**: ни в панель, ни в `state.db`. Его можно гонять сколько угодно.

### 9.10 Первая запись и запуск демона

```bash
# на узле: одна реальная синхронизация (перед записью делается бэкап шаблона)
xcb sync --config /etc/xray-client-balancer/config.yaml
xcb status --config /etc/xray-client-balancer/config.yaml
xcb clients  --config /etc/xray-client-balancer/config.yaml | head -30
```

Что нормально при первом `sync`:

* появится строка `Backup конфига: /var/lib/xray-client-balancer/backups/xray-template-*.json`
  — это точка отката;
* **ядро может перезапуститься один раз** — если в шаблоне ещё не было секции
  `observatory`/`burstObservatory`, покрывающей наши outbound'ы: сервис её добавит, а такие
  правки панель применяет только с рестартом ядра. Видно по pid ядра:

  ```bash
  # до sync и после него — pid и время старта
  pgrep -af '[x]ray-linux-amd64'; ps -o pid,etimes,lstart -p $(pgrep -f '[x]ray-linux-amd64' | head -1)
  ```

  pid сменился — был рестарт (это нормально ровно один раз); pid тот же — всё прошло
  hot-apply. Все последующие правки — только `routing`, то есть без рестарта; это же
  показывает и сама команда переноса: `Ядро: uptime A -> B (перезапуск: нет — hot-apply)`;
* повторный `sync` обязан сказать `No routing changes required` — это проверка идемпотентности.

Демон:

```bash
# на узле
install -m 0644 /opt/xray-client-balancer/systemd/xray-client-balancer.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xray-client-balancer
systemctl status xray-client-balancer --no-pager
journalctl -u xray-client-balancer -n 30 --no-pager
systemctl show -p NRestarts --value xray-client-balancer      # должно оставаться 0 и не расти
```

Про `ExecStart` в юните: по умолчанию там
`/opt/xray-client-balancer/.venv/bin/xray-client-balancer` (вариант А из §9.6). Если
зависимости ставились в `deps` (вариант Б), замените строку на

```
ExecStart=/usr/local/bin/xcb --config /etc/xray-client-balancer/config.yaml daemon
```

и сделайте `systemctl daemon-reload && systemctl restart xray-client-balancer`.
Глобальные ключи (`--config`, `--log-level`) парсер принимает и до, и после подкоманды —
проверено тестом `tests/test_cli.py`, поэтому обе формы `ExecStart` равнозначны.

Юнит ужесточён: `ProtectSystem=full`, `ReadWritePaths=/var/lib/xray-client-balancer
/etc/xray-client-balancer`, `MemoryAccounting=true`, `MemoryMax=256M` (измеренный RSS
сервиса ~40–45 МиБ; 256 МиБ — предохранитель, чтобы утечка перезапустила юнит, а не
съела память узла).

### 9.11 Приёмка после развёртывания (чек-лист)

```bash
# 1. распределение и живые балансировщики
xcb status | tail -25
#    ожидаем: "Clients: N", по каждой группе своё число, затем блок
#    "Balancers in Xray config:" со всеми client-balancer-* и секцией observatory, затем
#    "Live balancers (from running core):" со строками
#    "  client-balancer-N: running=True selected=[<primary>] override=-"

# 2. проверка маршрутов: её уже сделал сам сервис после записи — смотреть в журнале
journalctl -u xray-client-balancer --no-pager | grep -iE 'Route test|route test' | tail -10
#    ожидаем: "Route test: все группы маршрутизируются ожидаемо"
#    если написано "ядро ещё поднимается, маршрут не проверен" — это WARNING, повторите xcb sync

# 3. ядро принимает живой конфиг
/usr/local/x-ui/bin/xray-linux-amd64 run -test -c /usr/local/x-ui/bin/config.json   # Configuration OK.

# 4. наши балансировщики реально лежат в шаблоне панели
python3 - <<'PY'
import json, sqlite3
db = sqlite3.connect("file:/etc/x-ui/x-ui.db?mode=ro", uri=True)
tpl = json.loads(db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0])
routing = tpl.get("routing") or {}
print("balancers:", [b.get("tag") for b in routing.get("balancers") or []])
print("наши правила:", [r.get("balancerTag") for r in routing.get("rules") or [] if r.get("balancerTag")])
print("healthcheck:", "burstObservatory" if "burstObservatory" in tpl else ("observatory" if "observatory" in tpl else "нет"))
PY

# 5. идемпотентность: повторный sync не должен ничего менять
xcb sync; # -> No routing changes required

# 6. стабильность: 5 минут без изменений шаблона и без перезапусков ядра
python3 - <<'PY'
import hashlib, sqlite3, subprocess, time
for _ in range(15):
    db = sqlite3.connect("file:/etc/x-ui/x-ui.db?mode=ro", uri=True)
    tpl = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0]
    db.close()
    pid = subprocess.run(["pgrep", "-f", "[x]ray-linux-amd64"],
                         capture_output=True, text=True).stdout.strip()
    print(time.strftime("%T"), "sha256 шаблона:", hashlib.sha256(tpl.encode()).hexdigest()[:16],
          "| pid ядра:", pid or "—", flush=True)
    time.sleep(20)
PY
#    ожидаем: хеш шаблона не меняется, PID ядра не меняется

# 7. перенос клиента туда-обратно (боевая проверка команды, которую вы будете использовать)
xcb clients --filter <часть-email>          # узнать id/группу
xcb move <email> <другой-тег> --yes         # строки: hot-apply, routeTest подтверждён
xcb move <email> <исходный-тег> --yes       # вернуть как было

# 8. память и диск
xcb doctor --pid $(systemctl show -p MainPID --value xray-client-balancer)
```

### 9.12 Обновление кода на узле

Состояние (`state.db`), бэкапы и конфиг при обновлении не трогаются — обновляется только код.

```bash
# с рабочей машины: снимок текущей версии на узле (точка отката)
ssh root@HOST 'tar -czf /root/xray-client-balancer-$(date +%F-%H%M%S).tgz -C /opt xray-client-balancer'

# копирование новой версии (как в 9.4)
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.egg-info' HERE/ root@HOST:/opt/xray-client-balancer/

# на узле
systemctl restart xray-client-balancer
xcb sync --dry-run                # план должен быть пустым или объяснимым
xcb status
```

Откат кода: `systemctl stop xray-client-balancer; rm -rf /opt/xray-client-balancer;
mkdir -p /opt/xray-client-balancer && tar -xzf /root/xray-client-balancer-<ts>.tgz -C /opt;
systemctl start xray-client-balancer` — но проще распаковать в стороне и скопировать только
`.py`-файлы.

### 9.13 Откат конфигурации (если что-то пошло не так)

Три независимых уровня, от самого узкого к самому широкому:

1. **Правила сервиса в панели.** Сервис при остановке свои правила **не удаляет** — клиенты
   продолжают ходить по последней раскладке. Если нужно вернуть прежний routing: взять
   `backups/xray-template-*.json`, вынуть из него поле `template` (файл — обёртка
   `{"saved_at":..., "note":..., "template":{...}}`) и записать это значение в шаблон панели
   (`POST /panel/api/xray/update`, form-поле `xraySetting`), затем `xcb sync`. Быстрее — просто
   оставить сервис работать: он сам держит свой блок в согласованном виде.
2. **Шаблон панели целиком.** `python3`-скриптом (`sqlite3.connect("/etc/x-ui/x-ui.db",
   timeout=30)`, `PRAGMA busy_timeout=30000`) записать `settings.xrayTemplateConfig` из
   бэкапа §9.2 или из `backups.directory`, затем `systemctl restart x-ui` — панель
   перегенерирует `/usr/local/x-ui/bin/config.json`.
3. **БД панели.** `systemctl stop x-ui; cp -a /root/xui-backup-<ts>/. /etc/x-ui/; systemctl
   start x-ui`.

Правило то же, что и в §4: перед любой записью в панель берём бэкап, после — проверяем
`xray run -test` и состояние ядра.

### 9.14 Удаление сервиса с узла

```bash
# на узле
systemctl disable --now xray-client-balancer
rm -f /etc/systemd/system/xray-client-balancer.service && systemctl daemon-reload
rm -f /usr/local/bin/xcb
rm -rf /opt/xray-client-balancer /var/lib/xray-client-balancer
rm -rf /etc/xray-client-balancer            # там env с токеном — удалять обязательно
```

Что останется: правила и балансировщики сервиса в `routing` панели (он их не удаляет), и
сам API-токен в таблице `api_tokens`. Токен можно отключить (через python3, `sqlite3` на
узле обычно нет):

```bash
# на узле, с бэкапом БД (см. 9.2)
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("/etc/x-ui/x-ui.db", timeout=30)
db.execute("PRAGMA busy_timeout=30000")
db.execute("UPDATE api_tokens SET enabled=0 WHERE name='xcb'")
db.commit()
print("изменено строк:", db.total_changes)
PY
```

Если убирать раскладку совсем — верните шаблон из бэкапа (§9.13, пункт 2) и перезапустите `x-ui`.

### 9.15 Типичные проблемы при развёртывании

| симптом | причина | что делать |
|---|---|---|
| все `/panel/api/*` отвечают 404 с пустым телом | нет/неверный токен или неверный base path (панель скрывает эндпоинты) | сверить `webPort`/`webBasePath` (§9.5) и проверить curl'ом с токеном и без |
| `401` | токен заротировали (вызов `-getApiToken`) и не обновили env | получить токен заново и сразу обновить `/etc/xray-client-balancer/env`, затем `systemctl restart` |
| `sync` пишет `outbound 'X' отсутствует и в шаблоне, и в работающем конфиге` | тега нет в панели/подписках | создать outbound или поправить `balancers`/`fallback` |
| `balancers/list: obj не является массивом` и подобное | панель ответила не JSON (прокси, HTML) или не тем эндпоинтом | проверить URL/base path и что отвечает панель, а не nginx-заглушка |
| юнит циклически перезапускается, в журнале `unrecognized arguments` | на узле старая версия `main.py`, где глобальные ключи не принимались после подкоманды | обновить код (§9.12) |
| сразу после `sync` routeTest отвечает `xray is not running` / `connection refused` | ядро перезапускается панелью после первой записи, grpc-api ещё не слушает | подождать 5–30 с; сервис сам помечает это как «не проверено» (WARNING), а не как ошибку |
| CRITICAL `ядро Xray не поднялось после записи конфига — выполняется откат` | конфиг отвергнут ядром (нет outbound'а, битая strategy) | конфиг уже откачен; смотреть `xray/getXrayResult`, `journalctl -u x-ui`, правку шаблона |
| CRITICAL `сработал churn-предохранитель` | 3 цикла подряд конфиг приходилось «починять» (кто-то правит routing параллельно) | разобраться руками, затем `xcb sync --force-write` |
| `WARNING: конфиг изменился между чтением и записью` | параллельная правка конфига администратором | ничего: цикл пропущен, следующий догонит |
| `doctor` показывает WARN по WAL | checkpoint не проходит (долгая транзакция или второй процесс на той же БД) | `lsof /var/lib/xray-client-balancer/state.db*`, перезапустить юнит |

### 9.16 Чего сервис не делает (границы)

* не создаёт и не меняет inbounds, outbounds, клиентов панели — всё это руками в панели;
* не трогает чужие правила routing, чужие балансировщики, секции `log`/`dns`/`policy`/`api`/
  `stats`, inbounds и outbounds — ни при записи, ни при «починке»;
* не перераспределяет клиентов при перезапуске (sticky по внутреннему id) и никогда — при
  кратковременном падении узла;
* не удаляет свои правила при остановке (после `systemctl stop` раскладка остаётся рабочей);
* не перезапускает ядро на правках `routing` (hot-apply); единственный рестарт за жизнь
  узла — первая запись, если пришлось добавить healthcheck-секцию;
* не является мониторингом: `doctor` отвечает на вопрос «не течёт ли и не растёт ли», но
  сам ничего не шлёт — для алертов ставьте его в cron с проверкой кода выхода.

