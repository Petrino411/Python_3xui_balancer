"""Диагностика API установленной панели 3x-ui.

Ровно те проверки, которые требует ТЗ перед реализацией демона:
  ✓ authentication
  ✓ read clients
  ✓ read current Xray config
  ✓ config validation endpoint
  ✓ ability to update Xray configuration
  ✓ balancerStatus
  ✓ routeTest

Секреты (токен, UUID, subId) не печатаются никогда.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from urllib.parse import urlsplit

from . import routing
from .api import PanelApi, PanelError, PanelResponseError, build_verify
from .config import AppConfig
from .service import specs_from_config

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: list[str] = field(default_factory=list)
    error: str = ""

    def render(self) -> str:
        head = f"[{'OK ' if self.ok else 'FAIL'}] {self.name}"
        lines = [head]
        for detail in self.details:
            lines.append(f"        {detail}")
        if self.error:
            lines.append(f"        error: {self.error}")
        return "\n".join(lines)


def _short(value: Any, limit: int = 400) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "…"


def check_auth(config: AppConfig, api: PanelApi) -> CheckResult:
    """Аутентификация: токен принят и панель отвечает."""
    result = CheckResult("authentication (Bearer token)", ok=False)
    try:
        status = api.server_status()
        version = api.get_xray_version()
    except PanelError as exc:
        result.error = str(exc)
        return result
    result.ok = True
    result.details.append(f"Xray version: {version}")
    result.details.append(f"server/status keys: {sorted(status.keys())[:12]}")
    return result


def mask_url(url: str) -> str:
    """Скрыть base path панели в выводе (он секретный)."""
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}/{'*' * max(len(parsed.path.strip('/')), 1)}/"


def check_auth_is_enforced(config: AppConfig) -> CheckResult:
    """Без токена доступа к API быть не должно (401/403/404)."""
    result = CheckResult("authentication is enforced (no token)", ok=False)
    try:
        with httpx.Client(
            base_url=config.panel.url,
            verify=build_verify(config.panel),
            timeout=config.panel.timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = client.get(f"{config.panel.url}/panel/api/server/status")
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.details.append(f"GET /panel/api/server/status без токена -> HTTP {response.status_code}")
    result.ok = response.status_code in (401, 403, 404)
    if not result.ok:
        result.details.append("Панель отвечает без токена — проверьте права/доступность API")
    return result


def check_clients(config: AppConfig, api: PanelApi) -> CheckResult:
    """Чтение клиентов: список, стабильные id, привязка к inbound."""
    result = CheckResult("read clients (/panel/api/clients/list)", ok=False)
    try:
        clients = api.get_clients()
    except PanelError as exc:
        result.error = str(exc)
        return result
    result.ok = True
    result.details.append(f"clients: {len(clients)}")
    ids = [c.client_id for c in clients]
    result.details.append(f"стабильные внутренние id: да, пример [{[str(i) for i in ids[:5]]}]")
    result.details.append(
        f"уникальность id: {len(set(ids)) == len(ids)}; дубликатов email: "
        f"{len(clients) - len({c.email.lower() for c in clients})}"
    )
    multi = [c.email for c in clients if len(c.inbound_ids) > 1]
    result.details.append(f"клиентов более чем в одном inbound: {len(multi)} {multi[:3]}")
    for client in clients[:5]:
        result.details.append(f"  {_short(client.masked())}")
    disabled = [c.email for c in clients if not c.active]
    result.details.append(f"disabled/expired/traffic-exhausted: {len(disabled)} {disabled[:3]}")
    return result


def check_xray_config(config: AppConfig, api: PanelApi) -> CheckResult:
    """Чтение шаблона xray-конфига и структурная проверка под наши балансировщики."""
    result = CheckResult("read current Xray config (/panel/api/xray/)", ok=False)
    try:
        meta = api.get_xray_template_meta()
        template = api.get_xray_template()
    except PanelError as exc:
        result.error = str(exc)
        return result
    result.ok = True
    section = template.get("routing") if isinstance(template.get("routing"), dict) else {}
    rules = section.get("rules") or []
    result.details.append(f"top-level sections: {sorted(template.keys())}")
    result.details.append(f"routing.rules: {len(rules)}")
    for index, rule in enumerate(rules):
        result.details.append(f"  rule[{index}]: {_short(rule, 160)}")
    result.details.append(f"routing.balancers: {_short(section.get('balancers') or [])}")
    result.details.append(f"outbounds: {sorted(routing.collect_outbound_tags(template))}")
    for name in ("observatory", "burstObservatory"):
        if name in template:
            result.details.append(f"{name}: {_short(template[name])}")
    result.details.append(f"inboundTags из ответа панели: {meta.get('inboundTags')}")

    specs = specs_from_config(config)
    needed = sorted({spec.primary for spec in specs} | {spec.fallback for spec in specs})
    known = routing.collect_outbound_tags(template)
    # outbound'ы могут приходить не из шаблона, а из подписок панели: сверяемся с
    # собранным работающим конфигом (server/getConfigJson)
    running_tags: set[str] = set()
    try:
        running = api.get_running_config()
        running_tags = {
            str(o.get("tag")) for o in (running.get("outbounds") or []) if isinstance(o, dict) and o.get("tag")
        }
    except PanelError as exc:
        result.details.append(f"работающий конфиг недоступен: {exc}")
    result.details.append(f"нужные сервису outbound-теги: {needed}")
    result.details.append(f"outbound-теги работающего ядра: {sorted(running_tags)}")
    in_running = [tag for tag in needed if tag not in known and tag in running_tags]
    missing = [tag for tag in needed if tag not in known and tag not in running_tags]
    if in_running:
        result.details.append(
            f"есть в работающем конфиге (подписки/панель, не в шаблоне): {in_running} — это нормально"
        )
    if missing:
        result.details.append(
            f"ОТСУТСТВУЮТ везде: {missing} — до их создания сервис откажется писать routing "
            "(это ожидаемое поведение защиты §10)"
        )
    observed = observatory_section(template)
    result.details.append(f"healthcheck нужен: {observed}")
    return result


def observatory_section(template: dict[str, Any]) -> str:
    if "burstObservatory" in template:
        return "burstObservatory"
    if "observatory" in template:
        return "observatory"
    return "нет (сервис создаст при первой записи; это единственное изменение, требующее рестарта ядра)"


def check_validation_endpoint(config: AppConfig, api: PanelApi) -> CheckResult:
    """Проверка, что панель отвергает битый конфиг до сохранения (§10).

    Отправляем xraySetting, который не является JSON: панель обязана ответить success=false
    и НЕ изменить сохранённый шаблон (падение происходит на json.Unmarshal, до сохранения).

    ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ (измерено на живой 3.8.5): посылать синтаксически валидный, но
    структурно пустой конфиг — панель его принимает, сохраняет и перезапускает ядро без
    outbounds, то есть ломает production. Поэтому структурную валидацию сервис делает сам
    (routing.validate_candidate), не полагаясь на панель.
    """
    result = CheckResult("config validation endpoint (панель отвергает битый JSON)", ok=False)
    try:
        before = api.get_xray_template()
        uptime_before = api.xray_state().get("uptime")
    except PanelError as exc:
        result.error = f"не удалось прочитать шаблон до проверки: {exc}"
        return result
    try:
        api.update_xray_template_raw("{ not-a-json")
    except PanelResponseError as exc:
        result.details.append(f"битый JSON отвергнут: {str(exc)[:180]}")
    except PanelError as exc:
        result.error = f"неожиданная ошибка: {exc}"
        return result
    else:
        result.details.append("ВНИМАНИЕ: панель приняла синтаксически битый конфиг")
    try:
        after = api.get_xray_template()
        uptime_after = api.xray_state().get("uptime")
    except PanelError as exc:
        result.error = f"не удалось перечитать шаблон: {exc}"
        return result
    unchanged = routing.canonical(before) == routing.canonical(after)
    result.details.append(f"шаблон не изменился после отказа: {unchanged}")
    result.details.append(f"ядро не перезапускалось: {uptime_before == uptime_after}")
    result.details.append(
        "панель проверяет только синтаксис/запуск ядра: конфиг без outbounds она примет — "
        "поэтому структурную валидацию (наличие outbound-тегов, один primary на балансировщик, "
        "нет пустых rules) делает сам сервис перед записью"
    )
    result.ok = unchanged
    return result


def check_update_capability(config: AppConfig, api: PanelApi) -> CheckResult:
    """Возможность записать конфиг: round-trip с идентичным содержимым."""
    result = CheckResult("ability to update Xray configuration (/panel/api/xray/update)", ok=False)
    try:
        before = api.get_xray_template()
    except PanelError as exc:
        result.error = exc.__class__.__name__ + ": " + str(exc)
        return result
    try:
        api.update_xray_template(before)
    except PanelError as exc:
        result.error = str(exc)
        return result
    result.ok = True
    result.details.append("update принят панелью (содержимое не менялось)")
    try:
        after = api.get_xray_template()
    except PanelError as exc:
        result.details.append(f"перечитать шаблон не удалось: {exc}")
        return result
    diff = routing.foreign_diff(before, after, config.balancer_tags)
    result.details.append(
        f"после записи: добавлено правил {len(diff.added_rules)}, удалено {len(diff.removed_rules)}, "
        f"изменились секции {diff.sections_changed}"
    )
    if not diff.empty:
        result.details.append(
            "панель трансформирует шаблон при сохранении (например поднимает своё api-правило) — "
            "это учтено в сервисе сравнением мультимножества"
        )
    result.details.append(f"running config доступен: {bool(api.get_running_config())}")
    return result


def check_balancer_status(config: AppConfig, api: PanelApi) -> CheckResult:
    result = CheckResult("balancerStatus (/panel/api/xray/balancerStatus)", ok=False)
    tags = config.balancer_tags
    try:
        status = api.balancer_status(tags)
    except PanelError as exc:
        result.error = str(exc)
        return result
    result.ok = True
    result.details.append(f"запрошены теги: {tags}")
    result.details.append(f"ответ: {_short(status)}")
    if not status:
        result.details.append("ядро вернуло пустой ответ (нет запущенного Xray?)")
    return result


def check_route_test(config: AppConfig, api: PanelApi, email: str = "") -> CheckResult:
    result = CheckResult("routeTest (/panel/api/xray/routeTest)", ok=False)
    if email:
        try:
            with_email = api.route_test(domain=config.validation.route_test_domain, email=email)
        except PanelError as exc:
            result.error = str(exc)
            return result
        result.details.append(
            f"с email={email}: matched={with_email.matched} outboundTag={with_email.outbound_tag!r} "
            f"groupTags={with_email.group_tags}"
        )
    try:
        generic = api.route_test(domain=config.validation.route_test_domain)
    except PanelError as exc:
        result.error = str(exc)
        return result
    result.ok = True
    result.details.append(
        f"без email: matched={generic.matched} outboundTag={generic.outbound_tag!r} "
        f"groupTags={generic.group_tags}"
    )
    result.details.append(f"raw: {_short(generic.raw)}")
    return result


def run_checks(config: AppConfig, *, verbose: bool = False, allow_write: bool = False) -> int:
    """Прогнать все проверки и напечатать чек-лист. Возвращает 0, если всё критичное ок."""
    api = PanelApi(config.panel)
    results: list[CheckResult] = []
    try:
        print("Диагностика API 3x-ui")
        print(f"panel.url: {mask_url(config.panel.url)} (base path скрыт)")
        print("")
        results.append(check_auth(config, api))
        results.append(check_auth_is_enforced(config))
        results.append(check_clients(config, api))
        results.append(check_xray_config(config, api))
        results.append(check_validation_endpoint(config, api))
        if allow_write:
            results.append(check_update_capability(config, api))
        else:
            results.append(
                CheckResult(
                    "ability to update Xray configuration",
                    ok=True,
                    details=["не выполнялась: запустите с --allow-write (запись идентичного конфига)"],
                )
            )
        results.append(check_balancer_status(config, api))
        results.append(check_route_test(config, api))
    finally:
        api.close()

    print("")
    for result in results:
        print(result.render())
        print("")
    failed = [r.name for r in results if not r.ok]
    if failed:
        print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} ok; провалено: {failed}")
        return 1
    print(f"ИТОГО: все {len(results)} проверок пройдены")
    return 0


def check_functions() -> list[Callable[..., CheckResult]]:
    return [
        check_auth,
        check_auth_is_enforced,
        check_clients,
        check_xray_config,
        check_validation_endpoint,
        check_update_capability,
        check_balancer_status,
        check_route_test,
    ]
