"""Mock 3x-ui панели для интеграционных тестов (§42).

Поведение повторяет реальную панель 3.x:
  * без `Authorization: Bearer` API отвечает 404 (так устроен checkAPIAuth);
  * `xray/update` валидирует JSON и при ошибке отвечает success=false, ничего не сохраняя;
  * при сохранении панель нормализует шаблон: поднимает своё api-правило наверх и
    пересобирает JSON с сортировкой ключей (EnsureStatsRouting + Go-маршалинг);
  * `routeTest` отвечает тем outbound, который выбрал бы роутер по текущим правилам.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

TOKEN = "test-token-123"


def default_template() -> dict[str, Any]:
    return {
        "log": {"loglevel": "warning"},
        "api": {"services": ["HandlerService", "StatsService", "RoutingService"], "tag": "api"},
        "inbounds": [],
        "metrics": {"listen": "127.0.0.1:11111", "tag": "metrics_out"},
        "outbounds": [
            {"tag": "server-1", "protocol": "vless", "settings": {}},
            {"tag": "server-2", "protocol": "vless", "settings": {}},
            {"tag": "server-3", "protocol": "vless", "settings": {}},
            {"tag": "server-4", "protocol": "vless", "settings": {}},
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
        "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {
                    "type": "field",
                    "ip": ["geoip:private"],
                    "outboundTag": "direct",
                    "comment": "важное правило пользователя",
                },
            ],
        },
        "stats": {},
    }


def make_client(index: int, email: str | None = None, **overrides: Any) -> dict[str, Any]:
    email = email or f"user{index}@example"
    client = {
        "id": index,
        "email": email,
        "uuid": f"00000000-0000-0000-0000-{index:012d}",
        "subId": f"sub{index}",
        "totalGB": 0,
        "expiryTime": 0,
        "enable": True,
        "inboundIds": [2],
        "up": 0,
        "down": 0,
    }
    client.update(overrides)
    return client


class MockPanel:
    """Мини-панель 3x-ui."""

    def __init__(self, template: dict[str, Any] | None = None, clients: list[dict[str, Any]] | None = None) -> None:
        self.template: dict[str, Any] = json.loads(json.dumps(template or default_template()))
        self.clients: list[dict[str, Any]] = list(clients or [])
        self.mode: str = "ok"  # ok | unavailable | empty | malformed
        self.write_count = 0
        self.failed_writes = 0
        self.write_log: list[dict[str, Any]] = []
        self.route_override: Any = None
        # состояние ядра: имитируем панель 3.8.5 (server/status.xray.state)
        self.xray_state: str = "running"
        # если задано — routeTest отвечает success=false с этим сообщением (имитация стартующего ядра)
        self.route_failure: str = ""
        # предикат «с этим конфигом ядро не поднимется» — для проверки автоотката
        self.core_fails_for: Any = None
        # inbound'ы, которые панель добавляет в собранный работающий конфиг
        self.running_inbounds: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> str:
        panel = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: D102 - тишина в тестах
                return

            def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def _dispatch(self, method: str) -> None:
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                form = {k: v[0] for k, v in parse_qs(raw).items()} if raw else {}

                if panel.mode == "unavailable":
                    self._send(502, {"success": False, "msg": "bad gateway"})
                    return
                if not self._authorized():
                    self._send(404, None)
                    return

                if path == "/panel/api/clients/list":
                    if panel.mode == "malformed":
                        self._send(200, {"success": True, "obj": {"not": "a list"}})
                        return
                    if panel.mode == "empty":
                        self._send(200, {"success": True, "obj": []})
                        return
                    self._send(200, {"success": True, "obj": json.loads(json.dumps(panel.clients))})
                    return
                if path == "/panel/api/xray/":
                    self._send(
                        200,
                        {
                            "success": True,
                            "obj": {
                                "xraySetting": json.dumps(panel.template),
                                "inboundTags": json.dumps([i.get("tag") for i in panel.template.get("inbounds", [])]),
                                "outboundTestUrl": "https://www.gstatic.com/generate_204",
                            },
                        },
                    )
                    return
                if path == "/panel/api/xray/update":
                    panel.handle_update(form.get("xraySetting", ""), self._send)
                    return
                if path == "/panel/api/server/getConfigJson":
                    running = json.loads(json.dumps(panel.template))
                    running["inbounds"] = list(running.get("inbounds") or []) + list(panel.running_inbounds)
                    self._send(200, {"success": True, "obj": running})
                    return
                if path == "/panel/api/xray/balancerStatus":
                    tags = [t.strip() for t in form.get("tags", "").split(",") if t.strip()]
                    balancers = {b.get("tag"): b for b in panel.template.get("routing", {}).get("balancers", [])}
                    obj = {}
                    for tag in tags:
                        bal = balancers.get(tag)
                        obj[tag] = {
                            "tag": tag,
                            "running": bal is not None,
                            "override": "",
                            "selected": (bal or {}).get("selector", []),
                        }
                    self._send(200, {"success": True, "obj": obj})
                    return
                if path == "/panel/api/xray/routeTest":
                    # route_failure имитирует ответ ядра во время перезапуска (grpc-api не слушает)
                    if getattr(panel, "route_failure", ""):
                        self._send(200, {"success": False, "msg": panel.route_failure})
                    else:
                        self._send(200, {"success": True, "obj": panel.route_test(form)})
                    return
                if path == "/panel/api/server/status":
                    self._send(200, {"success": True, "obj": {
                        "cpu": 1.0,
                        "xray": {"state": panel.xray_state, "errorMsg": "" if panel.xray_state == "running" else "core failed"},
                        "appStats": {"uptime": 10},
                    }})
                    return
                if path == "/panel/api/server/getXrayVersion":
                    self._send(200, {"success": True, "obj": "26.9.9"})
                    return
                self._send(404, None)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockPanel":
        self.base_url = self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ behaviour

    def handle_update(self, raw: str, send) -> None:  # type: ignore[no-untyped-def]
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            self.failed_writes += 1
            send(200, {"success": False, "msg": "xray template config invalid"})
            return
        if not isinstance(parsed, dict):
            self.failed_writes += 1
            send(200, {"success": False, "msg": "xray template config invalid"})
            return
        self.template = self._panel_normalize(parsed)
        self.write_count += 1
        self.write_log.append(self.template)
        # панель перезапускает ядро на каждом принятом update — значит и состояние ядра
        # определяется тем, что мы только что записали
        if callable(self.core_fails_for):
            self.xray_state = "error" if self.core_fails_for(self.template) else "running"
        send(200, {"success": True, "msg": "ok"})

    @staticmethod
    def _panel_normalize(template: dict[str, Any]) -> dict[str, Any]:
        """Как реальная панель: api-правило наверх, ключи отсортированы, comment/enabled вырезаны."""
        normalized = json.loads(json.dumps(template))
        routing = normalized.get("routing")
        if isinstance(routing, dict):
            rules = routing.get("rules")
            if isinstance(rules, list) and rules:
                api_rule = next(
                    (r for r in rules if isinstance(r, dict) and r.get("inboundTag") == ["api"]),
                    {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                )
                rest = [r for r in rules if r is not api_rule]
                routing["rules"] = [api_rule, *rest]
        return json.loads(json.dumps(normalized, sort_keys=True))

    def route_test(self, form: dict[str, str]) -> dict[str, Any]:
        if callable(self.route_override):
            return self.route_override(form)
        email = form.get("email", "")
        routing = self.template.get("routing") or {}
        balancers = {b.get("tag"): b for b in routing.get("balancers") or []}
        for rule in routing.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if email and email in (rule.get("user") or []):
                tag = rule.get("balancerTag")
                bal = balancers.get(tag) or {}
                selector = bal.get("selector") or []
                return {
                    "matched": True,
                    "outboundTag": selector[0] if selector else "",
                    "groupTags": [tag] if tag else [],
                }
        return {"matched": False, "outboundTag": "", "groupTags": []}

    # ------------------------------------------------------------------ helpers

    def add_clients(self, *clients: dict[str, Any]) -> None:
        self.clients.extend(clients)

    def remove_client(self, client_id: int) -> None:
        self.clients = [c for c in self.clients if c["id"] != client_id]

    def managed_rules(self) -> list[dict[str, Any]]:
        return [
            r
            for r in (self.template.get("routing") or {}).get("rules") or []
            if isinstance(r, dict) and r.get("balancerTag")
        ]

    def assignment_of(self, email: str) -> str:
        for rule in self.managed_rules():
            if email in (rule.get("user") or []):
                return str(rule["balancerTag"])
        return ""

    def distribution(self) -> dict[str, int]:
        return {str(r["balancerTag"]): len(r.get("user") or []) for r in self.managed_rules()}
