# ТЗ: сервис автоматического распределения клиентов 3x-ui по балансировщикам

Нужно разработать отдельный Python-сервис для 3x-ui/Xray, который автоматически распределяет клиентов между тремя рабочими группами маршрутизации и обеспечивает стабильное закрепление клиента за одной группой.

## 1. Общая задача

Есть сервер 3x-ui/Xray.

Есть 4 исходящих сервера:

```text
server-1
server-2
server-3
server-4
```

Первые три используются как основные:

```text
server-1 → balancer-1
server-2 → balancer-2
server-3 → balancer-3
```

`server-4` является резервным сервером и используется как fallback для всех трёх балансировщиков.

Логика должна быть:

```text
Balancer 1:
primary = server-1
fallback = server-4

Balancer 2:
primary = server-2
fallback = server-4

Balancer 3:
primary = server-3
fallback = server-4
```

Клиенты должны распределяться между:

```text
balancer-1
balancer-2
balancer-3
```

примерно поровну.

Важно: **балансировать нужно клиентов, а не отдельные соединения**.

То есть конкретный клиент должен стабильно иметь:

```text
client-A → balancer-1
```

и все его TCP/UDP-соединения должны попадать в этот balancer.

Нельзя, чтобы отдельные подключения одного клиента распределялись по разным основным серверам.

---

## 2. Требуемый результат

Например, при 10 клиентах:

```text
balancer-1:
user1
user4
user7
user10

balancer-2:
user2
user5
user8

balancer-3:
user3
user6
user9
```

То есть:

```text
4 / 3 / 3
```

Допустимая разница между количеством клиентов в группах:

```text
не более 1 клиента
```

при нормальном состоянии всех трёх основных серверов.

---

## 3. Sticky-привязка клиента

Это важнейшее требование.

После назначения клиента на группу:

```text
user123 → balancer-2
```

он должен оставаться в `balancer-2`.

Нельзя при каждом цикле сервиса перераспределять всех пользователей только ради достижения идеального количества.

Например, было:

```text
B1 = 10
B2 = 10
B3 = 10
```

добавился один пользователь:

```text
B1 = 11
B2 = 10
B3 = 10
```

Это нормально.

Сервис не должен переставлять существующих пользователей.

Новый пользователь должен назначаться в одну из групп с минимальным количеством пользователей.

---

## 4. Добавление нового клиента

Сервис должен автоматически обнаруживать появление нового клиента в 3x-ui.

Алгоритм:

```text
получить список клиентов

найти клиентов, которых нет в локальной базе распределения

для каждого нового клиента:

    определить количество клиентов:
        balancer-1
        balancer-2
        balancer-3

    выбрать группу с минимальным количеством

    сохранить назначение
```

Например:

```text
B1 = 15
B2 = 14
B3 = 15

new_user → B2
```

После чего:

```text
15 / 15 / 15
```

---

## 5. Удаление клиента

Сервис обязан корректно переживать удаление клиента из 3x-ui.

Например было:

```text
user1 → B1
user2 → B2
user3 → B3
```

`user2` удалили через интерфейс 3x-ui.

На следующем цикле сервис должен:

```text
обнаружить отсутствие user2
удалить его из своей локальной БД
удалить его из генерируемого routing rule
```

Никаких ошибок вида:

```text
KeyError
NoneType
client not found
```

быть не должно.

Удаление клиента **не должно вызывать полное перераспределение остальных пользователей**.

Например:

```text
до:
10 / 10 / 10

удалён один из B2:

10 / 9 / 10
```

Это допустимое состояние.

Не надо автоматически переносить кого-то из B1/B3 в B2.

Следующий новый клиент просто попадёт в B2.

---

## 6. Изменение email клиента

Нужно учитывать возможное изменение email/идентификатора клиента.

Если API 3x-ui предоставляет стабильный внутренний ID/UUID клиентской записи, использовать его как основной ключ в локальной БД.

Email использовать как значение для Xray routing `user`.

При изменении email:

```text
client internal ID тот же
```

назначение балансировщика должно сохраниться.

Например:

```text
old@example → B2
```

пользователь переименован:

```text
new@example
```

должно стать:

```text
new@example → B2
```

а не восприниматься как удаление + создание нового пользователя.

Если стабильного внутреннего ID API не предоставляет, агент должен изучить API 3x-ui и выбрать максимально стабильный идентификатор.

---

## 7. Routing rules

Сервис должен автоматически поддерживать правила Xray вида:

```json
{
  "type": "field",
  "user": [
    "user1@example",
    "user4@example",
    "user7@example"
  ],
  "balancerTag": "balancer-1"
}
```

```json
{
  "type": "field",
  "user": [
    "user2@example",
    "user5@example",
    "user8@example"
  ],
  "balancerTag": "balancer-2"
}
```

```json
{
  "type": "field",
  "user": [
    "user3@example",
    "user6@example",
    "user9@example"
  ],
  "balancerTag": "balancer-3"
}
```

Правила маршрутизации Xray проверяются сверху вниз — первое совпадение выигрывает.

Поэтому сервис должен аккуратно управлять **только своими правилами** и не ломать остальные пользовательские routing rules.

---

## 8. Управление только собственным блоком

Нельзя каждый раз полностью перезаписывать весь `routing.rules`.

Необходимо придумать способ однозначно определять три правила, принадлежащие сервису.

Например через фиксированную позицию/структуру либо другой устойчивый механизм.

Требование:

```text
manual rule 1
manual rule 2

AUTO managed balancer-1 rule
AUTO managed balancer-2 rule
AUTO managed balancer-3 rule

manual rule 3
manual rule 4
```

При обновлении клиентов сервис должен менять только `AUTO managed` правила.

Пользовательские правила не должны исчезать или менять порядок.

Перед записью новой конфигурации обязательно перечитывать актуальный конфиг 3x-ui.

Нельзя работать по старой копии конфигурации, потому что пользователь мог изменить её вручную.

---

## 9. Защита от race condition

Возможен сценарий:

```text
сервис прочитал конфиг

↓ 2 секунды

пользователь изменил routing через UI

↓ 1 секунда

сервис сохранил старый конфиг
```

В результате изменения пользователя потеряются.

Это недопустимо.

Перед записью:

1. получить актуальный Xray config;
2. изменить только принадлежащие сервису элементы;
3. проверить конфигурацию;
4. записать.

Если API предоставляет revision/hash/version — использовать optimistic locking.

Если нет — минимизировать интервал read → write.

---

## 10. Проверка конфигурации до применения

Перед перезагрузкой/сохранением конфигурации необходимо запускать штатную проверку Xray config через API 3x-ui, если доступна.

Алгоритм:

```text
generate new config
        ↓
validate
        ↓
valid?
 ├─ no  → ничего не применять + error log
 └─ yes → сохранить
```

Нельзя применять заведомо невалидный JSON/Xray config.

---

## 11. Балансировщики

Предполагается наличие трёх balancer:

```text
client-balancer-1
client-balancer-2
client-balancer-3
```

Каждый должен иметь свой основной outbound.

Например:

```text
client-balancer-1:
    selector → server-1
    fallback → server-4

client-balancer-2:
    selector → server-2
    fallback → server-4

client-balancer-3:
    selector → server-3
    fallback → server-4
```

Если конфигурация 3x-ui/Xray позволяет сделать balancer-to-balancer fallback более корректным способом, использовать поддерживаемый официальный механизм.

---

## 12. Основные серверы не должны балансироваться друг с другом

Критически важно:

```text
balancer-1
```

не должен иметь selector:

```text
server-1
server-2
server-3
```

Иначе Xray снова начнёт выбирать outbound отдельно для соединений.

Нужно:

```text
balancer-1 → server-1
balancer-2 → server-2
balancer-3 → server-3
```

а `server-4` используется только как fallback.

---

## 13. Поведение при падении основного сервера

Если:

```text
server-2 DOWN
```

клиенты группы:

```text
balancer-2
```

должны автоматически использовать:

```text
server-4
```

При этом сервис **не должен переписывать пользователей из B2 в B1 или B3**.

То есть логическое назначение остаётся:

```text
user5 → balancer-2
```

Просто сам balancer временно работает:

```text
balancer-2
       ↓
server-4
```

Это очень важное разделение:

```text
client assignment
```

и:

```text
current physical outbound
```

— разные вещи.

---

## 14. Возврат после восстановления

Когда:

```text
server-2
```

снова становится рабочим, группа должна автоматически вернуться:

```text
balancer-2 → server-2
```

без изменения client assignments.

Желательно использовать штатный fallback/healthcheck Xray, а не реализовывать постоянное переключение в Python, если штатный механизм работает корректно.

Python-сервис отвечает в первую очередь за **распределение клиентов по группам**.

Xray отвечает за:

```text
primary alive → primary
primary down → fallback
```

---

## 15. Четвёртый сервер

`server-4` — общий аварийный outbound.

При нормальной работе:

```text
server-4
```

не должен получать обычный пользовательский трафик.

Он должен использоваться только если один из:

```text
server-1
server-2
server-3
```

недоступен.

Допустимый сценарий:

```text
server-1 DOWN
server-3 DOWN

B1 → server-4
B2 → server-2
B3 → server-4
```

То есть резервный сервер может временно обслуживать несколько групп одновременно.

---

## 16. Нельзя делать автоматическое перераспределение при кратковременном падении

Например:

```text
server-1 упал на 30 секунд
```

нельзя делать:

```text
всех клиентов B1 переписать в B2/B3
```

Потому что после восстановления получится churn.

Именно для этого существует fallback server.

Логическая карта:

```text
client → balancer
```

должна оставаться стабильной.

---

## 17. Persistency

Сервис должен хранить назначения клиентов между перезапусками.

Можно использовать SQLite.

Например таблица:

```sql
client_assignments

client_id
email
balancer_tag
created_at
updated_at
```

Пример:

```text
42 | alice@example | client-balancer-1
47 | bob@example   | client-balancer-3
51 | john@example  | client-balancer-2
```

После перезапуска:

```text
systemctl restart xray-client-balancer
```

распределение должно остаться тем же.

---

## 18. SQLite

Желательно использовать:

```text
SQLite
```

без отдельной PostgreSQL/MySQL зависимости.

Хранилище:

```text
/var/lib/xray-client-balancer/state.db
```

Создавать автоматически.

Использовать транзакции.

---

## 19. Первый запуск

На первом запуске БД пустая.

Сервис получает всех существующих клиентов.

И распределяет их максимально равномерно.

Например 11 клиентов:

```text
B1 = 4
B2 = 4
B3 = 3
```

Но распределение должно быть детерминированным.

Например отсортировать клиентов по стабильному ID:

```text
client1 → B1
client2 → B2
client3 → B3
client4 → B1
...
```

После сохранения назначения больше не менять.

---

## 20. Поведение после удаления большого числа клиентов

Например:

```text
B1 = 50
B2 = 50
B3 = 50
```

Удалили:

```text
30 клиентов из B1
```

Стало:

```text
20 / 50 / 50
```

Сервис НЕ должен автоматически переселять 20 пользователей из B2/B3.

Новые пользователи должны сначала заполнять B1.

Это сделано специально ради стабильности IP пользователей.

---

## 21. Опциональный rebalance

Добавить отдельную ручную команду:

```bash
xray-client-balancer rebalance
```

или:

```bash
python -m app rebalance
```

которая уже **намеренно** перераспределяет всех клиентов максимально равномерно.

Но она никогда не должна выполняться автоматически.

Перед выполнением вывести:

```text
Current:

B1: 20
B2: 50
B3: 50

After rebalance:

B1: 40
B2: 40
B3: 40

90 client assignments will change.
```

И требовать:

```text
--yes
```

для применения.

---

## 22. Dry-run

Обязательно добавить:

```bash
--dry-run
```

Например:

```bash
xray-client-balancer sync --dry-run
```

Вывод:

```text
Found clients: 31

New:
  anna@example → client-balancer-2
  mike@example → client-balancer-3

Removed:
  old@example

Changes to routing:
  B1: 10
  B2: 11
  B3: 10

No changes applied.
```

---

## 23. Работа сервиса

Основной daemon loop:

```text
startup

↓
authenticate

↓
load local assignments

↓
fetch clients

↓
reconcile assignments

↓
if changes:
    update managed routing rules

↓
sleep

↓
repeat
```

Интервал:

```text
30 секунд
```

с возможностью настройки.

Не требуется обновление каждую секунду.

---

## 24. Idempotency

Критически важно.

Если состояние уже правильное:

```text
local DB == clients == routing
```

следующий цикл не должен:

- менять config;
- рестартовать Xray;
- делать unnecessary API calls на запись;
- менять timestamps назначения;
- менять порядок клиентов без причины.

Лог:

```text
No changes. 42 clients, distribution 14/14/14.
```

---

## 25. Восстановление после ручного изменения routing

Если пользователь случайно удалил одно из управляемых правил:

```text
client-balancer-2 rule
```

сервис должен на следующем reconcile восстановить его из локальной БД.

Если пользователь изменил другие routing rules, сервис не должен их трогать.

---

## 26. Если один клиент присутствует в нескольких inbound

Такой клиент всё равно должен иметь **одно назначение балансировщика**:

```text
client-X → B2
```

независимо от количества inbound.

Не создавать несколько assignment для одного и того же логического клиента.

---

## 27. Неактивные/disabled клиенты

Нужно изучить API и решить поведение.

Предпочтительный вариант:

Клиент остаётся в assignment DB даже если временно:

```text
disabled
expired
traffic exhausted
```

Потому что при повторном включении он должен получить тот же balancer.

Полностью удалять assignment только если клиент реально удалён из 3x-ui.

---

## 28. Стабильность сортировки

В JSON-массиве пользователей одного правила желательно всегда использовать стабильный порядок:

```text
sort by email
```

или стабильный client ID.

Это позволит избежать бессмысленных diff конфигурации.

---

## 29. API 3x-ui

Использовать официальный REST API.

Авторизация предпочтительно:

```http
Authorization: Bearer <API_TOKEN>
```

Не хранить:

```text
username/password
```

если можно использовать API token.

---

## 30. Секреты

Конфигурация:

```text
/etc/xray-client-balancer/config.yaml
```

Пример:

```yaml
panel:
  url: "https://127.0.0.1:2053"
  api_token: "${XRAY_BALANCER_API_TOKEN}"
  verify_tls: true

balancers:
  - tag: "client-balancer-1"
    primary: "server-1"

  - tag: "client-balancer-2"
    primary: "server-2"

  - tag: "client-balancer-3"
    primary: "server-3"

fallback:
  outbound: "server-4"

sync:
  interval_seconds: 30

state:
  database: "/var/lib/xray-client-balancer/state.db"
```

API token не должен храниться открытым в git.

Поддержать env:

```bash
XRAY_BALANCER_API_TOKEN=
```

---

## 31. Ошибки API

Если 3x-ui временно недоступен:

```text
Connection refused
Timeout
502
503
```

сервис не должен падать.

Нужно:

```text
log error
retry later
```

Например exponential backoff:

```text
5s
10s
20s
30s
```

с верхним лимитом.

После восстановления API продолжить обычную работу.

---

## 32. Нельзя очищать routing при ошибке получения клиентов

Это критически важно.

Если API вернул ошибку или пустой/невалидный ответ:

```text
НЕ считать, что клиентов стало 0.
```

Нельзя сделать:

```text
API failed
↓
clients=[]
↓
delete all routing users
```

Изменять routing можно только после **успешного подтверждённого получения полного списка клиентов**.

---

## 33. Atomic update

Процесс обновления должен выглядеть:

```text
fetch config
        ↓
build modified config in memory
        ↓
validate JSON
        ↓
Xray config test
        ↓
save/apply
```

При любой ошибке старый рабочий config должен остаться активным.

---

## 34. Backup

Перед первым изменением конфигурации после запуска делать backup исходного routing config.

Например:

```text
/var/lib/xray-client-balancer/backups/
```

Не создавать backup каждые 30 секунд.

Создавать при фактическом изменении с разумной ротацией, например последние 20 экземпляров.

---

## 35. Logging

Использовать стандартный Python `logging`.

Пример:

```text
INFO  Sync started
INFO  Received 42 clients
INFO  Distribution: B1=14 B2=14 B3=14
INFO  No routing changes required
```

Новый пользователь:

```text
INFO  New client john@example
INFO  Assigned john@example -> client-balancer-2
INFO  Routing updated successfully
```

Удаление:

```text
INFO  Removed client old@example from local state
INFO  Routing updated successfully
```

Ошибка:

```text
ERROR Unable to fetch clients: HTTP 502
WARNING Keeping previous state and routing unchanged
```

---

## 36. systemd

Создать unit:

```text
/etc/systemd/system/xray-client-balancer.service
```

Пример поведения:

```ini
Restart=always
RestartSec=5
```

Сервис должен запускаться после сети.

Необходимо предоставить команды:

```bash
systemctl enable --now xray-client-balancer
systemctl status xray-client-balancer
journalctl -u xray-client-balancer -f
```

---

## 37. Python

Использовать:

```text
Python 3.11+
```

Желательно зависимости:

```text
httpx
pydantic
PyYAML
```

SQLite через стандартный:

```text
sqlite3
```

Не использовать тяжёлый framework без необходимости.

---

## 38. Структура проекта

Предпочтительно:

```text
xray-client-balancer/
├── pyproject.toml
├── README.md
├── config.example.yaml
├── systemd/
│   └── xray-client-balancer.service
├── src/
│   └── xray_client_balancer/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── api.py
│       ├── database.py
│       ├── allocator.py
│       ├── routing.py
│       └── models.py
└── tests/
    ├── test_allocator.py
    ├── test_reconcile.py
    ├── test_deletions.py
    └── test_routing.py
```

---

## 39. Логика allocator

Allocator должен быть полностью отделён от API.

Пример API функции:

```python
assign_new_clients(
    existing_assignments,
    current_clients,
    balancers
)
```

Выход:

```python
{
    "client-A": "client-balancer-1",
    "client-B": "client-balancer-2",
    ...
}
```

Выбор для нового клиента:

```python
min(
    balancers,
    key=lambda b: (
        assignment_count[b],
        deterministic_balancer_order[b]
    )
)
```

То есть при одинаковой загрузке результат должен быть детерминированным.

---

## 40. Никакого random

Не использовать:

```python
random.choice()
```

при распределении клиентов.

Иначе после восстановления БД/первой синхронизации распределение будет непредсказуемым.

---

## 41. Тесты

Обязательно unit tests.

### 3 клиента

```text
1 / 1 / 1
```

### 4 клиента

```text
2 / 1 / 1
```

### 10 клиентов

```text
4 / 3 / 3
```

### Добавление клиента

Было:

```text
4 / 3 / 3
```

добавился пользователь:

```text
4 / 4 / 3
```

или эквивалентно по выбранному deterministic tie-break.

### Удаление

Было:

```text
4 / 4 / 4
```

удалили двух из B1:

```text
2 / 4 / 4
```

Остальные назначения не меняются.

### Повторный sync

Должен дать:

```text
0 changes
```

### Перезапуск

После reload SQLite назначения полностью сохраняются.

### API failure

Routing не изменяется.

### Invalid config

Routing не применяется.

---

## 42. Интеграционный тест

Добавить mock 3x-ui API.

Сценарий:

```text
1. API возвращает 9 клиентов
2. сервис создаёт 3/3/3
3. API возвращает 10 клиентов
4. становится 4/3/3
5. удалить клиента из B2
6. становится 4/2/3
7. добавить двух
8. новые идут сначала в B2
```

Проверять, что старые assignment не изменились.

---

## 43. Route test

Если текущая версия 3x-ui предоставляет route-test API, использовать его после изменения конфигурации как дополнительную проверку.

Например случайно выбрать по одному клиенту из каждой группы:

```text
client from B1 → expected balancer/outbound B1
client from B2 → expected B2
client from B3 → expected B3
```

Если route test показывает неожиданное направление:

```text
log CRITICAL
```

Но не пытаться бесконечно переписывать конфиг.

---

## 44. Status command

Нужна команда:

```bash
xray-client-balancer status
```

Пример:

```text
Clients: 43

client-balancer-1: 15
client-balancer-2: 14
client-balancer-3: 14

Fallback:
server-4

Last successful sync:
2026-09-26 17:42:11

Last config update:
2026-09-26 17:30:02
```

---

## 45. List command

```bash
xray-client-balancer list
```

Например:

```text
client-balancer-1
  alice@example
  ivan@example
  user17@example

client-balancer-2
  ...
```

---

## 46. Возможность исключения клиентов

Добавить конфигурацию:

```yaml
exclude_clients:
  - admin@example
  - service@example
```

Такие клиенты не должны попадать в managed routing rules.

Также желательно:

```yaml
exclude_regex:
  - "^test-"
```

---

## 47. Не ломать ручную маршрутизацию

Если клиент явно матчится более приоритетным пользовательским routing rule, сервис не должен автоматически удалять его.

Нужно документировать порядок правил.

Managed client rules должны находиться в явно определённой позиции routing.

Желательно иметь настройку:

```yaml
routing:
  insert_after_rule: "some-marker"
```

или устойчивую альтернативу, которую агент сможет реализовать с учётом формата 3x-ui.

---

## 48. Что НЕ нужно делать

Не нужно:

- менять outbound на каждом соединении;
- использовать `random`;
- использовать `roundRobin`;
- использовать `leastLoad` для распределения клиентов;
- регулярно переставлять существующих клиентов;
- напрямую редактировать SQLite базы 3x-ui;
- перезаписывать весь config на основе старой локальной копии;
- менять assignment клиента при временном падении primary.

---

## 49. Главный принцип архитектуры

В системе должны быть два независимых уровня:

```text
УРОВЕНЬ 1 — пользователь

client
   ↓
sticky assignment
   ↓
balancer-1 / balancer-2 / balancer-3
```

и:

```text
УРОВЕНЬ 2 — физический маршрут

balancer-1 → server-1
                  ↓ fail
               server-4

balancer-2 → server-2
                  ↓ fail
               server-4

balancer-3 → server-3
                  ↓ fail
               server-4
```

Python управляет **уровнем 1**.

Xray/Observatory/fallback управляет **уровнем 2**.

Это принципиально.

---

## 50. Итоговый сценарий

Имеем:

```text
30 клиентов
```

Сервис создаёт:

```text
B1 → 10
B2 → 10
B3 → 10
```

Появилось ещё 5:

```text
B1 → 12
B2 → 12
B3 → 11
```

Упал `server-2`:

```text
B1 → server-1
B2 → server-4
B3 → server-3
```

При этом assignments всё ещё:

```text
12 / 12 / 11
```

Восстановился `server-2`:

```text
B2 → server-2
```

Ни один клиент не был перепривязан.

Удалили 7 клиентов:

```text
B1 → 9
B2 → 9
B3 → 8
```

Добавили нового:

```text
→ B3
```

Получаем:

```text
9 / 9 / 9
```

Именно это должно быть конечным поведением.

---

# Отдельное указание агенту

**Перед реализацией изучить актуальный OpenAPI текущей установленной версии 3x-ui. Не придумывать endpoint'ы.**

Сначала агент должен сделать маленький diagnostic script:

```text
test_api.py
```

который проверит:

```text
✓ authentication
✓ read clients
✓ read current Xray config
✓ config validation endpoint
✓ ability to update Xray configuration
✓ balancerStatus
✓ routeTest
```

И только после подтверждения реальных схем запросов/ответов начинать писать основной daemon.

Это важно: нельзя угадывать структуру API по примерам из интернета — нужно использовать OpenAPI именно установленной панели.

Также обязательное требование: **первый запуск только в `--dry-run`**, пока не будет подтверждено, что сервис правильно нашёл клиентов и распределил их по 3 группам.
