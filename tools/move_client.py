"""Перевести клиента на другой балансировщик (совместимая обёртка над `xcb move`).

Раньше это был самостоятельный скрипт; теперь вся логика живёт в библиотеке
(`xray_client_balancer.ops` + команда `move`), а здесь остаётся только точка входа.

    python3 move_client.py Test@example client-balancer-1            # план
    python3 move_client.py Test@example client-balancer-1 --apply    # выполнить
    python3 move_client.py Test@example client-balancer-3 --apply    # вернуть обратно

Почему это безопасно: sticky-назначение хранится в state.db, поэтому перенос — это
правка одной строки + один sync; из правил routing меняются только те, что содержат
этого клиента, а такие правки панель применяет hot-apply — ядро НЕ перезапускается.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_CONFIG = "/etc/xray-client-balancer/config.yaml"
# Каталоги, откуда берём код и зависимости: сначала боевой, затем каталог самого скрипта.
BOOTSTRAP_ROOTS = (
    "/opt/xray-client-balancer",
    str(Path(__file__).resolve().parent.parent),
)


def _bootstrap() -> None:
    """Добавить в sys.path код сервиса и каталог зависимостей (на узле venv может не быть)."""
    for root in BOOTSTRAP_ROOTS:
        for sub in ("deps", "src"):
            candidate = Path(root) / sub
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.append(str(candidate))


def build_argv(args: argparse.Namespace) -> list[str]:
    """Собрать аргументы CLI сервиса. Разбор и печать делает сама команда `move`."""
    argv = ["move", args.email, args.target, "--config", args.config]
    if args.id is not None:
        argv += ["--id", str(args.id)]
    if args.apply:
        argv.append("--yes")
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Свап клиента между балансировщиками (план по умолчанию, --apply для записи)",
        epilog="Эквивалент: xcb move <email> <тег> [--yes]",
    )
    parser.add_argument("email", help="email клиента (как в панели) или подстрока")
    parser.add_argument("target", help="тег балансировщика, куда перевести (можно сокращение: 1/2/3)")
    parser.add_argument("--apply", action="store_true", help="действительно изменить и записать")
    parser.add_argument("--id", type=int, default=None, help="внутренний id клиента")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="путь к config.yaml сервиса")
    args = parser.parse_args(argv)

    _bootstrap()
    from xray_client_balancer.main import main as cli_main

    return cli_main(build_argv(args))


if __name__ == "__main__":
    raise SystemExit(main())
