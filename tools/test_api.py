#!/usr/bin/env python3
"""test_api.py — диагностический скрипт для установленной панели 3x-ui.

ТЗ требует подтвердить реальные схемы запросов/ответов ДО запуска демона:

    ✓ authentication
    ✓ read clients
    ✓ read current Xray config
    ✓ config validation endpoint
    ✓ ability to update Xray configuration
    ✓ balancerStatus
    ✓ routeTest

Запуск (в скрипте на хосте с панелью):

    XRAY_BALANCER_API_TOKEN=<token> ./tools/test_api.py --config config.yaml
    XRAY_BALANCER_API_TOKEN=<token> ./tools/test_api.py --url https://127.0.0.1:7119/ --token-env

Опции:
    --allow-write   дополнительно выполнить запись конфига (идентичное содержимое)
    --verbose       печатать больше деталей

Токен никогда не печатается; UUID/subId клиентов маскируются.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xray_client_balancer.config import AppConfig, ConfigError, load_config  # noqa: E402
from xray_client_balancer.diagnostics import run_checks  # noqa: E402
from xray_client_balancer.main import setup_logging  # noqa: E402


def build_inline_config(url: str, token: str | None, verify_tls: bool) -> AppConfig:
    """Минимальная конфигурация «на лету», если yaml недоступен."""
    raw = {
        "panel": {"url": url, "api_token": token or "", "verify_tls": verify_tls},
        "balancers": [
            {"tag": "client-balancer-1", "primary": "server-1"},
            {"tag": "client-balancer-2", "primary": "server-2"},
            {"tag": "client-balancer-3", "primary": "server-3"},
        ],
        "fallback": {"outbound": "server-4"},
    }
    return AppConfig.model_validate(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Диагностика API 3x-ui")
    parser.add_argument("--config", default=None, help="путь к config.yaml (по умолчанию ищем в стандартных местах)")
    parser.add_argument("--url", default=None, help="URL панели (вместе с base path)")
    parser.add_argument("--token", default=None, help="API-токен (лучше через переменную окружения)")
    parser.add_argument("--allow-write", action="store_true", help="разрешить тестовую запись конфига")
    parser.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат панели")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)

    try:
        if args.url:
            config = build_inline_config(args.url, args.token, not args.insecure)
        else:
            candidates = [args.config] if args.config else [
                os.environ.get("XCB_CONFIG"),
                "/etc/xray-client-balancer/config.yaml",
                str(Path(__file__).resolve().parent.parent / "config.yaml"),
            ]
            path = next((c for c in candidates if c and Path(c).exists()), None)
            if path is None:
                print("Не найден config.yaml: укажите --config или --url", file=sys.stderr)
                return 2
            config = load_config(path)
    except ConfigError as exc:
        print(f"Конфигурация: {exc}", file=sys.stderr)
        return 2

    if args.insecure:
        config.panel.verify_tls = False
    if args.token:
        config.panel.api_token = args.token

    return run_checks(config, verbose=args.verbose, allow_write=args.allow_write)


if __name__ == "__main__":
    raise SystemExit(main())
