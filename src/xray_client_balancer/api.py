"""Клиент REST API 3x-ui.

Все endpoint'ы взяты из OpenAPI, который отдаёт сама установленная панель
(`GET <base>/panel/api/openapi.json`) и сверены с исходниками 3x-ui 3.8.5:

    GET  /panel/api/clients/list              -> список клиентов (id, email, inboundIds, traffic)
    POST /panel/api/xray/                     -> шаблон xray-конфига (xraySetting)
    POST /panel/api/xray/update               -> сохранить шаблон (form: xraySetting)
    GET  /panel/api/xray/getXrayResult        -> вывод ядра Xray (ошибки запуска)
    GET  /panel/api/server/getConfigJson      -> собранный работающий конфиг
    POST /panel/api/xray/balancerStatus       -> Live-состояние балансировщиков (form: tags)
    POST /panel/api/xray/routeTest            -> какой outbound выберет ядро для соединения
    GET  /panel/api/server/status             -> состояние машины
    GET  /panel/api/server/getXrayVersion     -> версия ядра

Авторизация: `Authorization: Bearer <token>` (api_tokens панели, scope=admin).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from .config import PanelConfig
from .models import PanelClient

logger = logging.getLogger(__name__)


class PanelError(RuntimeError):
    """Базовая ошибка обращения к панели."""


class PanelUnavailable(PanelError):
    """Панель недоступна: connection refused/timeout/5xx — состояние менять нельзя."""


class PanelResponseError(PanelError):
    """Панель ответила, но ответ невалиден или success=false."""


class PanelAuthError(PanelError):
    """Токен отвергнут (401/403)."""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


@dataclass
class RouteTestResult:
    matched: bool
    outbound_tag: str
    group_tags: list[str]
    raw: dict[str, Any]


def build_verify(config: PanelConfig) -> Any:
    """Проверка TLS: либо выключена, либо цепочка по своему CA (с опцией без проверки имени).

    Панель часто слушает loopback с сертификатом на публичный домен, поэтому имя
    хоста может не совпадать; цепочку при этом проверять всё равно полезно.
    """
    import ssl

    if not config.verify_tls:
        return False
    if not config.ca_bundle:
        return True
    # create_default_context(cafile=...) НЕ подхватывает системные корни, поэтому
    # сначала берём системное хранилище, затем добавляем свой CA — иначе цепочка
    # Let's Encrypt обрывается на «unable to get issuer certificate».
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=config.ca_bundle)
    context.check_hostname = config.tls_verify_hostname
    return context


class PanelApi:
    """Тонкая обёртка над REST API 3x-ui с ретраями и защитой от «пустых» ответов."""

    def __init__(self, config: PanelConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._token = config.resolve_token()
        self._client = client or httpx.Client(
            base_url=config.url,
            verify=build_verify(config),
            timeout=config.timeout_seconds,
            headers={"Authorization": f"Bearer {self._token}"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ low level

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PanelApi":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self._config.url}/panel/api/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Выполнить запрос с экспоненциальным backoff (5/10/20/30c, верхний лимит из конфига)."""
        attempts = self._config.retries + 1
        delay = self._config.backoff_seconds
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(method, self._url(path), **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = PanelUnavailable(f"{method} {path}: {type(exc).__name__}: {exc}")
            else:
                if response.status_code in (401, 403):
                    raise PanelAuthError(
                        f"{method} {path}: панель отвергла токен (HTTP {response.status_code})"
                    )
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = PanelUnavailable(
                        f"{method} {path}: HTTP {response.status_code}"
                    )
                elif response.status_code >= 400:
                    raise PanelError(f"{method} {path}: HTTP {response.status_code}")
                else:
                    return self._parse(response, f"{method} {path}")

            if attempt < attempts:
                logger.warning(
                    "API error (%s), retry %d/%d in %.0fs: %s",
                    method,
                    attempt,
                    attempts - 1,
                    delay,
                    last_error,
                )
                time.sleep(delay)
                delay = min(delay * 2, self._config.max_backoff_seconds)
        raise last_error or PanelUnavailable(f"{method} {path}: неизвестная ошибка")

    @staticmethod
    def _parse(response: httpx.Response, context: str) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PanelResponseError(f"{context}: ответ не JSON: {exc}") from exc
        if not isinstance(payload, dict) or "success" not in payload:
            raise PanelResponseError(f"{context}: неожиданная структура ответа")
        if not payload.get("success"):
            raise PanelResponseError(f"{context}: success=false, msg={payload.get('msg')!r}")
        return payload.get("obj")

    # ------------------------------------------------------------------ endpoints

    def get_clients(self) -> list[PanelClient]:
        """Полный подтверждённый список клиентов панели.

        Ответ валидируется: ошибка/невалидный ответ -> исключение, и вызывающий
        код обязан сохранить прежнее состояние (нельзя считать, что клиентов 0).
        """
        obj = self._request("GET", "clients/list")
        if not isinstance(obj, list):
            raise PanelResponseError("clients/list: obj не является массивом клиентов")
        parsed: list[PanelClient] = []
        for item in obj:
            if not isinstance(item, dict):
                raise PanelResponseError("clients/list: элемент списка не объект")
            client_id = item.get("id")
            email = item.get("email")
            if client_id is None or email is None:
                raise PanelResponseError(
                    "clients/list: у клиента нет поля id/email — панель вернула неожиданную схему"
                )
            inbounds = item.get("inboundIds", item.get("inbound_ids"))
            if isinstance(inbounds, (int, str)):
                inbounds = [inbounds]
            parsed.append(
                PanelClient(
                    client_id=int(client_id),
                    email=str(email),
                    enable=bool(item.get("enable", True)),
                    inbound_ids=tuple(int(i) for i in _as_list(inbounds)),
                    uuid=str(item.get("uuid") or ""),
                    sub_id=str(item.get("subId") or item.get("sub_id") or ""),
                    total_bytes=int(item.get("totalGB") or item.get("total_bytes") or 0),
                    up=int(item.get("up") or 0),
                    down=int(item.get("down") or 0),
                    expiry_time_ms=int(item.get("expiryTime") or item.get("expiry_time") or 0),
                )
            )
        return parsed

    def get_xray_template(self) -> dict[str, Any]:
        """Текущий шаблон xray-конфига (routing/outbounds/log/...).

        Реальная панель 3.8.5 отдаёт `obj` как JSON-строку вида
        {"xraySetting": {...}, "inboundTags": [...], ...} — проверено на живой панели
        (см. tools/test_api.py). Поддерживаем и вариант, когда obj — объект.
        """
        return self._extract_template(self._request("POST", "xray/", data={}))

    @staticmethod
    def _extract_template(obj: Any) -> dict[str, Any]:
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except json.JSONDecodeError as exc:
                raise PanelResponseError(f"xray/: obj не парсится как JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise PanelResponseError(f"xray/: неожиданный тип ответа {type(obj).__name__}")
        if "xraySetting" in obj:
            raw = obj["xraySetting"]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise PanelResponseError(f"xray/xraySetting не парсится: {exc}") from exc
            if not isinstance(raw, dict):
                raise PanelResponseError("xray/xraySetting: корень не объект")
            return raw
        # панель без обёртки: сразу конфиг
        if any(k in obj for k in ("inbounds", "outbounds", "routing", "log", "api")):
            return obj
        raise PanelResponseError("xray/: ответ без поля xraySetting")

    def get_xray_template_meta(self) -> dict[str, Any]:
        obj = self._request("POST", "xray/", data={})
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except json.JSONDecodeError:
                return {}
        return obj if isinstance(obj, dict) else {}

    def update_xray_template(
        self, template: dict[str, Any], outbound_test_url: str | None = None
    ) -> None:
        """Сохранить шаблон. Панель сама валидирует конфиг (CheckXrayConfig) и применяет его.

        ВАЖНО (измерено на живой панели 3.8.5): панель перезапускает ядро при КАЖДОМ
        принятом update, даже если содержимое не изменилось. Поэтому вызывать только
        тогда, когда конфиг действительно нужно менять.
        """
        self.update_xray_template_raw(json.dumps(template, ensure_ascii=False), outbound_test_url)

    def update_xray_template_raw(self, raw: str, outbound_test_url: str | None = None) -> None:
        """Отправить xraySetting как есть (нужно для проверки валидации панели)."""
        data: dict[str, Any] = {"xraySetting": raw}
        if outbound_test_url:
            data["outboundTestUrl"] = outbound_test_url
        self._request("POST", "xray/update", data=data)

    def get_xray_result(self) -> str:
        obj = self._request("GET", "xray/getXrayResult")
        return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)

    def get_running_config(self) -> dict[str, Any]:
        """Собранный конфиг, который реально работает (нужен для локального `xray -test`)."""
        obj = self._request("GET", "server/getConfigJson")
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, str):
            try:
                parsed = json.loads(obj)
            except json.JSONDecodeError as exc:
                raise PanelResponseError(f"getConfigJson не парсится: {exc}") from exc
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def balancer_status(self, tags: Iterable[str]) -> dict[str, Any]:
        """Live-состояние балансировщиков в работающем ядре."""
        tag_list = [t for t in tags if t]
        if not tag_list:
            return {}
        obj = self._request("POST", "xray/balancerStatus", data={"tags": ",".join(tag_list)})
        return obj if isinstance(obj, dict) else {}

    def route_test(
        self,
        domain: str = "",
        ip: str = "",
        port: int = 0,
        network: str = "tcp",
        email: str = "",
        inbound_tag: str = "",
    ) -> RouteTestResult:
        """Спросить у ядра, какой outbound оно выберет для синтетического соединения."""
        data = {"domain": domain, "ip": ip, "network": network}
        if port:
            data["port"] = str(port)
        if email:
            data["email"] = email
        if inbound_tag:
            data["inboundTag"] = inbound_tag
        obj = self._request("POST", "xray/routeTest", data=data)
        if not isinstance(obj, dict):
            raise PanelResponseError("routeTest: obj не объект")
        group_tags = obj.get("groupTags") or []
        if isinstance(group_tags, str):
            group_tags = [group_tags]
        return RouteTestResult(
            matched=bool(obj.get("matched")),
            outbound_tag=str(obj.get("outboundTag") or ""),
            group_tags=[str(t) for t in group_tags],
            raw=obj,
        )

    def get_xray_version(self) -> str:
        obj = self._request("GET", "server/getXrayVersion")
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            return str(obj.get("version") or json.dumps(obj, ensure_ascii=False))
        return str(obj)

    def server_status(self) -> dict[str, Any]:
        obj = self._request("GET", "server/status")
        return obj if isinstance(obj, dict) else {}

    def xray_state(self) -> dict[str, Any]:
        """Состояние ядра по данным панели: {state, errorMsg, version, uptime}.

        Измерено на 3.8.5: server/status.xray = {"state":"running","errorMsg":"","version":...},
        а appStats.uptime — секунды работы ядра (годится как детектор перезапуска).
        """
        status = self.server_status()
        state = status.get("xray")
        result = dict(state) if isinstance(state, dict) else {}
        stats = status.get("appStats")
        if isinstance(stats, dict) and "uptime" in stats:
            result["uptime"] = stats["uptime"]
        return result

    def xray_is_running(self) -> bool:
        """Работает ли ядро. Если панель не сообщает состояние — считаем, что работает.

        Ошибиться в сторону «ядро живое» безопаснее: ложный откат сам является записью
        в конфиг и перезапуском ядра.
        """
        state = self.xray_state()
        if "state" not in state:
            return True
        return str(state.get("state")) == "running"

    def wait_until_xray_ready(self, timeout: float = 30.0, interval: float = 1.0) -> bool:
        """Дождаться, пока ядро поднимется после перезапуска (§37).

        Панель перезапускает ядро на каждом принятом update; пока оно стартует,
        balancerStatus/routeTest отвечают «xray is not running» — это НЕ ошибка маршрутизации.
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        while True:
            try:
                if self.xray_is_running():
                    return True
            except PanelError:
                pass
            if _time.monotonic() >= deadline:
                return False
            _time.sleep(interval)
