"""Глобальные ключи CLI должны работать и до, и после подкоманды (§ ТЗ: systemd-юнит)."""
from __future__ import annotations

import json

from xray_client_balancer.main import build_parser, main


def test_config_before_subcommand() -> None:
    args = build_parser().parse_args(["--config", "/etc/xcb/config.yaml", "sync"])
    assert args.command == "sync"
    assert args.config == "/etc/xcb/config.yaml"


def test_config_after_subcommand() -> None:
    args = build_parser().parse_args(["daemon", "--config", "/etc/xcb/config.yaml"])
    assert args.command == "daemon"
    assert args.config == "/etc/xcb/config.yaml"


def test_log_level_after_subcommand_and_defaults() -> None:
    args = build_parser().parse_args(["status", "--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"
    assert args.config is None
    assert build_parser().parse_args(["status"]).log_level == "INFO"


def test_subcommand_options_still_work() -> None:
    args = build_parser().parse_args(["sync", "--dry-run", "--config", "c.yaml"])
    assert args.dry_run is True
    assert args.config == "c.yaml"
    assert build_parser().parse_args(["rebalance", "--yes"]).yes is True


def test_move_subcommand_forms() -> None:
    two_positional = build_parser().parse_args(["move", "anna@example", "client-balancer-2"])
    assert two_positional.clients == ["anna@example", "client-balancer-2"]
    assert two_positional.to_tag is None and two_positional.yes is False

    many = build_parser().parse_args(["move", "--to", "1", "a@example", "b@example", "--yes"])
    assert many.to_tag == "1"
    assert many.clients == ["a@example", "b@example"]
    assert many.yes is True

    by_id = build_parser().parse_args(["move", "--id", "17", "--to", "client-balancer-3"])
    assert by_id.id == 17 and by_id.clients == []

    auto = build_parser().parse_args(["move", "anna@example", "--auto", "--no-verify"])
    assert auto.auto is True and auto.no_verify is True

    # алиас assign — та же команда
    assert build_parser().parse_args(["assign", "a@example", "2"]).command == "assign"


def test_global_keys_work_after_new_subcommands() -> None:
    for argv in (
        ["move", "a@example", "2", "--config", "/etc/xcb/config.yaml"],
        ["clients", "--config", "/etc/xcb/config.yaml"],
        ["balancers", "--config", "/etc/xcb/config.yaml"],
        ["doctor", "--config", "/etc/xcb/config.yaml"],
    ):
        args = build_parser().parse_args(argv)
        assert args.config == "/etc/xcb/config.yaml"


def test_clients_balancers_doctor_options() -> None:
    clients = build_parser().parse_args(["clients", "--filter", "an", "--group", "2", "--offline"])
    assert clients.filter == "an" and clients.group == "2" and clients.offline is True

    balancers = build_parser().parse_args(["balancers", "--live"])
    assert balancers.live is True

    doctor = build_parser().parse_args(["doctor", "--pid", "1234", "--rss-warn-mib", "64"])
    assert doctor.pid == 1234 and doctor.rss_warn_mib == 64.0
    assert build_parser().parse_args(["doctor"]).pid is None


def test_main_reports_missing_token_as_config_error(tmp_path, monkeypatch) -> None:
    """Нет токена панели — код 2 и внятное сообщение, а не трейсбек."""
    monkeypatch.delenv("XRAY_BALANCER_API_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        # сервис читает YAML; JSON — его подмножество, так что дампа достаточно
        json.dumps(
            {
                "panel": {"url": "http://127.0.0.1:7119/", "verify_tls": False},
                "balancers": [{"tag": "client-balancer-1", "primary": "server-1"}],
                "fallback": {"outbound": "server-4"},
                "state": {"database": str(tmp_path / "state.db")},
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "status"]) == 2
