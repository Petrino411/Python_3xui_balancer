"""Первичная настройка сервиса: найти исходящие панели, дать выбрать, собрать конфиг.

Зачем модуль: раньше конфиг писался руками — с тегами, которые надо было заранее
выяснить из БД панели. Теперь всё, что можно узнать у панели, сервис узнаёт сам:

  1. `xcb init` читает шаблон Xray (`/panel/api/xray/`) и работающий конфиг
     (`/panel/api/server/getConfigJson`) — в 3.8.x outbound'ы могут приходить из
     подписок панели и в шаблоне их не будет, поэтому источников именно два;
  2. показывает список исходящих с протоколом и адресом;
  3. спрашивает номера тех, что станут балансировщиками, и номер общего резерва;
  4. записывает `config.yaml` (теги уже подставлены) и сразу делает первую
     синхронизацию — балансировщики и правила routing создаёт сам сервис.

URL панели тоже не обязательно вводить руками: если сервис стоит на том же узле,
что панель, он берёт `webPort`/`webBasePath`/сертификат из `/etc/x-ui/x-ui.db`
(read-only). Токен панели в коде и в конфиге не хранится — только имя переменной
окружения, значение берётся из `/etc/xray-client-balancer/env`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .api import PanelApi, PanelError
from .config import AppConfig, ConfigError, DEFAULT_CONFIG_PATH, PanelConfig
from .models import BALANCER_TAG_PREFIX

ENV_PANEL_URL = "XRAY_BALANCER_PANEL_URL"
ENV_TOKEN_NAME = "XRAY_BALANCER_API_TOKEN"
PANEL_DB = "/etc/x-ui/x-ui.db"
DEFAULT_STRATEGY = "leastLoad"
XRAY_BINARY = "/usr/local/x-ui/bin/xray-linux-amd64"

# Служебные исходящие панели и ядра: их не предлагаем как балансировщики, но
# показываем по `xcb outbounds --all` (например direct иногда нужен как резерв).
INTERNAL_TAGS = {
    "direct",
    "freedom",
    "blackhole",
    "blocked",
    "dns",
    "dns-out",
    "api",
    "metrics",
    "metrics_out",
    "bittorrent",
    "traffic",
}
INTERNAL_PROTOCOLS = {"freedom", "blackhole", "dns"}

CONFIG_HEADER = """\
# Конфигурация xray-client-balancer.
# Файл создан командой `xcb init`; правьте значения и повторяйте `xcb sync`.
# Токен панели здесь не хранится: api_token берётся из окружения
# (${XRAY_BALANCER_API_TOKEN}) — значение лежит в /etc/xray-client-balancer/env.
"""


class SetupError(RuntimeError):
    """Настройку нельзя продолжить. Сообщение печатается пользователю как есть."""


@dataclass(frozen=True)
class OutboundInfo:
    """Один исходящий панели: то, из чего собираются балансировщики."""

    tag: str
    protocol: str = ""
    address: str = ""
    port: str = ""
    origin: str = ""
    internal: bool = False

    @property
    def endpoint(self) -> str:
        if not self.address:
            return ""
        return f"{self.address}:{self.port}" if self.port else self.address

    def row(self) -> tuple[str, str, str, str]:
        return (self.tag, self.protocol, self.endpoint, self.origin)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "protocol": self.protocol,
            "address": self.address,
            "port": self.port,
            "origin": self.origin,
            "internal": self.internal,
        }


# ------------------------------------------------------------------ поиск исходящих


def _outbound_objects(container: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    outbounds = (container or {}).get("outbounds")
    if not isinstance(outbounds, list):
        return []
    return [o for o in outbounds if isinstance(o, dict) and str(o.get("tag") or "")]


def _port_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def outbound_endpoint(protocol: str, settings: Any) -> tuple[str, str]:
    """Адрес и порт исходящего — для показа человеку, а не для работы сервиса.

    Схема разная у разных протоколов (`vnext` у vless/vmess, `servers` у trojan/
    shadowsocks/socks), поэтому перебираем известные варианты и в непонятном
    случае просто возвращаем пусто: список всё равно покажет тег и протокол.
    """
    if not isinstance(settings, dict):
        return "", ""
    for key in ("vnext", "servers"):
        entries = settings.get(key)
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            entry = entries[0]
            return str(entry.get("address") or ""), _port_text(entry.get("port"))
    if protocol == "wireguard":
        peers = settings.get("peers")
        if isinstance(peers, list) and peers and isinstance(peers[0], dict):
            endpoint = str(peers[0].get("endpoint") or "")
            host, _, port = endpoint.rpartition(":")
            return (host or endpoint), port
    if protocol == "freedom":
        return "", ""
    return "", ""


def discover_outbounds(
    template: Mapping[str, Any] | None,
    running: Mapping[str, Any] | None = None,
    *,
    include_internal: bool = False,
) -> list[OutboundInfo]:
    """Слить исходящие шаблона и работающего ядра в один список без дублей.

    Порядок — как в шаблоне; теги, которых в шаблоне нет (подписки панели), идут
    следом. Детали берём из работающего конфига, если тег есть и там: у подписок
    он собран полностью, у шаблона настройки могут быть пустыми.
    """
    order: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    origins: dict[str, set[str]] = {}
    for source, container in (("шаблон", template), ("ядро", running)):
        for obj in _outbound_objects(container):
            tag = str(obj["tag"])
            if tag not in merged:
                order.append(tag)
                merged[tag] = {}
            merged[tag].update(obj)
            origins.setdefault(tag, set()).add(source)

    result: list[OutboundInfo] = []
    for tag in order:
        obj = merged[tag]
        protocol = str(obj.get("protocol") or "")
        address, port = outbound_endpoint(protocol, obj.get("settings"))
        internal = tag.lower() in INTERNAL_TAGS or protocol.lower() in INTERNAL_PROTOCOLS
        if internal and not include_internal:
            continue
        result.append(
            OutboundInfo(
                tag=tag,
                protocol=protocol,
                address=address,
                port=port,
                origin="+".join(name for name in ("шаблон", "ядро") if name in origins[tag]),
                internal=internal,
            )
        )
    return result


def fetch_outbounds(api: PanelApi, *, include_internal: bool = False) -> list[OutboundInfo]:
    """Прочитать исходящие у панели. Работающий конфиг — best effort, шаблон обязателен."""
    template = api.get_xray_template()
    running: dict[str, Any] | None = None
    try:
        running = api.get_running_config()
    except PanelError:
        running = None
    return discover_outbounds(template, running, include_internal=include_internal)


# ------------------------------------------------------------------ показ и выбор


def render_outbounds(items: Sequence[OutboundInfo], *, numbered: bool = True) -> str:
    """Список исходящих: номер, тег, протокол, адрес, откуда взялся."""
    if not items:
        return "  (исходящих нет)"
    tag_width = max(len("тег"), *(len(i.tag) for i in items))
    proto_width = max(len("протокол"), *(len(i.protocol) for i in items))
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        prefix = f"{index:>3}) " if numbered else "     "
        mark = "  служебный" if item.internal else ""
        lines.append(
            f"{prefix}{item.tag:<{tag_width}}  {item.protocol:<{proto_width}}  "
            f"{item.endpoint:<28} [{item.origin}]{mark}".rstrip()
        )
    return "\n".join(lines)


def parse_selection(text: str, tags: Sequence[str], *, allow_empty: bool = False) -> list[str]:
    """Разобрать ответ вида '1,3', '1-3', 'all' или теги в список тегов.

    Ошибка вместо догадки: непонятный номер в этой команде означал бы
    ненастроенный балансировщик, а не «наверное, это он».
    """
    raw = (text or "").strip()
    if not raw:
        if allow_empty:
            return []
        raise SetupError("ничего не выбрано")
    lowered = raw.lower()
    if lowered in {"all", "все", "*"}:
        if not tags:
            raise SetupError("список пуст")
        return list(tags)

    chosen: list[str] = []
    for token in raw.replace(",", " ").split():
        if "-" in token and token.replace("-", "").isdigit():
            start_text, _, end_text = token.partition("-")
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            for index in range(start, end + 1):
                if not 1 <= index <= len(tags):
                    raise SetupError(f"номера {index} нет в списке (всего {len(tags)})")
                if tags[index - 1] not in chosen:
                    chosen.append(tags[index - 1])
            continue
        if token.isdigit():
            index = int(token)
            if not 1 <= index <= len(tags):
                raise SetupError(f"номера {index} нет в списке (всего {len(tags)})")
            if tags[index - 1] not in chosen:
                chosen.append(tags[index - 1])
            continue
        exact = next((t for t in tags if t == token), None) or next(
            (t for t in tags if t.lower() == token.lower()), None
        )
        if exact is None:
            raise SetupError(
                f"'{token}' не номер и не тег из списка: {', '.join(tags)}"
            )
        if exact not in chosen:
            chosen.append(exact)
    if not chosen and not allow_empty:
        raise SetupError("ничего не выбрано")
    return chosen


def ask(prompt: str) -> str:  # pragma: no cover - интерактив, в тестах подменяется
    return input(prompt)


def _ask_or(prompt: str, ask_fn: Callable[[str], str]) -> str:
    try:
        return ask_fn(prompt)
    except (EOFError, KeyboardInterrupt) as exc:  # pragma: no cover - Ctrl-D/Ctrl-C
        raise SetupError("ввод прерван — конфиг не изменён") from exc


def choose_primaries(
    items: Sequence[OutboundInfo],
    *,
    ask_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] = print,
) -> list[str]:
    """Спросить, какие исходящие станут балансировщиками (балансировщик на каждый)."""
    ask_fn = ask_fn or ask
    tags = [item.tag for item in items]
    print_fn("")
    print_fn("Шаг 1. Какие исходящие сделать балансировщиками?")
    print_fn("  Номера через запятую или диапазон (например 1,3 или 1-3).")
    print_fn("  На каждый выбранный будет создан свой client-balancer-N с общим резервом.")
    while True:
        answer = _ask_or("  Номера: ", ask_fn)
        try:
            return parse_selection(answer, tags)
        except SetupError as exc:
            print_fn(f"  Не понял: {exc}")


def choose_fallback(
    items: Sequence[OutboundInfo],
    primaries: Sequence[str],
    *,
    ask_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] = print,
) -> str:
    """Спросить общий резерв: на него уходят все группы, если их primary недоступен."""
    ask_fn = ask_fn or ask
    candidates = [item for item in items if item.tag not in set(primaries)]
    if not candidates:
        raise SetupError(
            "резерв не из чего выбрать: все исходящие уже заняты балансировщиками — "
            "создайте в панели ещё один исходящий для fallback"
        )
    print_fn("")
    print_fn("Шаг 2. Какой исходящий сделать общим резервом (fallback)?")
    print_fn("  Он подхватывает трафик, когда недоступен основной исходящий группы.")
    print_fn("")
    print_fn(render_outbounds(candidates))
    tags = [item.tag for item in candidates]
    if len(candidates) == 1:
        print_fn("")
        print_fn(f"  Единственный вариант: {candidates[0].tag}")
        return candidates[0].tag
    while True:
        answer = _ask_or("  Номер: ", ask_fn)
        try:
            picked = parse_selection(answer, tags)
        except SetupError as exc:
            print_fn(f"  Не понял: {exc}")
            continue
        if len(picked) != 1:
            print_fn("  Нужен ровно один исходящий.")
            continue
        return picked[0]


# ------------------------------------------------------------------ конфиг


def balancer_tags(count: int) -> list[str]:
    return [f"{BALANCER_TAG_PREFIX}{index}" for index in range(1, count + 1)]


def build_config_document(
    panel_url: str,
    primaries: Sequence[str],
    fallback: str,
    *,
    strategy: str | None = DEFAULT_STRATEGY,
    verify_tls: bool = True,
    ca_bundle: str | None = None,
    tls_verify_hostname: bool | None = None,
    state_database: str | None = None,
    backups_directory: str | None = None,
) -> dict[str, Any]:
    """Собрать валидный конфиг из выбора пользователя.

    Документ строит сама модель конфига (`AppConfig`), а не строковый шаблон:
    тогда `xcb init` физически не может записать конфиг, который сервис потом
    не примет.
    """
    if not primaries:
        raise SetupError("нужен хотя бы один балансировщик")
    if fallback in set(primaries):
        raise SetupError(f"исходящий '{fallback}' выбран и балансировщиком, и резервом")

    from .config import (
        BackupConfig,
        BalancerConfig,
        FallbackConfig,
        StateConfig,
        ValidationConfig,
    )

    if tls_verify_hostname is None:
        # панель слушает loopback, а сертификат обычно выписан на публичное имя
        tls_verify_hostname = ca_bundle is None

    config = AppConfig(
        panel=PanelConfig(
            url=panel_url,
            api_token="${" + ENV_TOKEN_NAME + "}",
            verify_tls=verify_tls,
            ca_bundle=ca_bundle,
            tls_verify_hostname=tls_verify_hostname,
        ),
        balancers=[
            BalancerConfig(**{"tag": tag, "primary": primary, "strategy": strategy})
            for tag, primary in zip(balancer_tags(len(primaries)), primaries)
        ],
        fallback=FallbackConfig(outbound=fallback),
        state=StateConfig(database=state_database) if state_database else StateConfig(),
        backups=BackupConfig(directory=backups_directory) if backups_directory else BackupConfig(),
        validation=_default_validation(),
    )
    return config.model_dump(mode="json")


def _default_validation() -> Any:
    """Проверять кандидата локальным ядром, если сервис стоит рядом с панелью."""
    from .config import ValidationConfig

    validation = ValidationConfig()
    if Path(XRAY_BINARY).exists():
        validation.local_xray_test = True
    return validation


def render_config(path: str | os.PathLike[str], document: Mapping[str, Any]) -> str:
    body = yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=False, default_flow_style=False)
    return CONFIG_HEADER + body


def write_config(path: str | os.PathLike[str], document: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(target, document), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - экзотическая ФС
        pass
    return target


# ------------------------------------------------------------------ панель


def detect_panel_settings(db_path: str | os.PathLike[str] = PANEL_DB) -> dict[str, str]:
    """Прочитать настройки панели из её БД (read-only). Пусто, если БД нет."""
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:  # pragma: no cover - БД занята/битая
        return {}
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('webPort','webBasePath','webCertFile','webKeyFile')"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(key): str(value or "") for key, value in rows}


def detect_panel_url(db_path: str | os.PathLike[str] = PANEL_DB) -> tuple[str, str | None] | None:
    """URL панели по её же настройкам: (url, путь к сертификату). None — не определить."""
    settings = detect_panel_settings(db_path)
    port = (settings.get("webPort") or "").strip()
    if not port:
        return None
    base = (settings.get("webBasePath") or "").strip("/")
    cert = (settings.get("webCertFile") or "").strip()
    scheme = "https" if cert else "http"
    url = f"{scheme}://127.0.0.1:{port}/" + (f"{base}/" if base else "")
    return url, (cert or None)


def resolve_panel_config(
    *,
    config: AppConfig | None = None,
    url: str | None = None,
    insecure: bool = False,
    ask_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] = print,
) -> PanelConfig:
    """Определить, куда и с каким токеном ходить в панель.

    Порядок: аргумент `--panel-url`, уже существующий конфиг, переменная окружения
    `XRAY_BALANCER_PANEL_URL`, автоопределение по БД панели на этом же узле, и в
    самом конце — вопрос человеку. Токен спрашиваем только наличием: значение
    берётся из окружения и в конфиг не пишется.
    """
    ask_fn = ask_fn or ask
    ca_bundle: str | None = None
    verify_tls = True
    tls_verify_hostname = True

    if url:
        panel_url = url
    elif config is not None:
        panel_url = config.panel.url
        ca_bundle = config.panel.ca_bundle
        verify_tls = config.panel.verify_tls
        tls_verify_hostname = config.panel.tls_verify_hostname
    elif os.environ.get(ENV_PANEL_URL, "").strip():
        panel_url = os.environ[ENV_PANEL_URL].strip()
    else:
        detected = detect_panel_url()
        if detected is None:
            panel_url = _ask_or(
                "URL панели (например https://127.0.0.1:21868/AbCdEf/): ", ask_fn
            ).strip()
            if not panel_url:
                raise SetupError(
                    "URL панели не определён: передайте --panel-url или положите "
                    f"{ENV_PANEL_URL}=... в /etc/xray-client-balancer/env"
                )
        else:
            panel_url, ca_bundle = detected
            print_fn(f"Панель найдена на этом узле: {panel_url}")
            if ca_bundle:
                print_fn(f"Сертификат панели: {ca_bundle}")

    token = os.environ.get(ENV_TOKEN_NAME, "").strip()
    if not token and config is not None:
        token = (config.panel.api_token or "").strip()
    if not token:
        raise SetupError(
            f"не задан {ENV_TOKEN_NAME}. Возьмите токен в панели (Настройки → "
            "Безопасность → API-токены) и положите строку "
            f"{ENV_TOKEN_NAME}=<токен> в /etc/xray-client-balancer/env"
        )

    return PanelConfig(
        url=panel_url,
        api_token=token,
        verify_tls=not insecure and verify_tls,
        ca_bundle=None if insecure else ca_bundle,
        tls_verify_hostname=False if insecure else tls_verify_hostname,
    )


def open_panel(config: PanelConfig) -> PanelApi:
    try:
        return PanelApi(config)
    except ConfigError as exc:  # pragma: no cover - токен уже проверен выше
        raise SetupError(str(exc)) from exc


def dump_outbounds(items: Iterable[OutboundInfo]) -> str:
    return json.dumps([item.as_dict() for item in items], ensure_ascii=False, indent=2)
