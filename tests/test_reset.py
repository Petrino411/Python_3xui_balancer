"""Полная перенастройка с нуля (`xcb reset`) и очистка чужого в конфиге."""

from __future__ import annotations

import json
from pathlib import Path

from mock_panel import make_client

from xray_client_balancer import routing
from xray_client_balancer.database import StateStore
from xray_client_balancer.main import main
from xray_client_balancer.models import BalancerSpec

from conftest import make_config


def write_config_file(panel, tmp_path: Path, **overrides) -> Path:
    """Урезанный конфиг на диске — как его увидит `reset` при запуске из CLI."""
    document = make_config(panel.base_url, tmp_path, **overrides).model_dump(mode="json")
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def sync_direct(config, panel) -> None:
    """Один цикл синхронизации мимо CLI (готовим состояние перед reset)."""
    from xray_client_balancer.api import PanelApi
    from xray_client_balancer.service import BalancerService

    store = StateStore(config.state.database)
    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
    finally:
        api.close()
        store.close()


# ------------------------------------------------------------------ strip_managed


def test_strip_managed_removes_only_ours() -> None:
    specs = [BalancerSpec("client-balancer-1", "server-1", "server-4")]
    template = {
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "user": ["a@example"], "balancerTag": "client-balancer-1"},
                {"type": "field", "domain": ["example.com"], "outboundTag": "direct"},
            ],
            "balancers": [
                {"tag": "client-balancer-1", "selector": ["server-1"]},
                {"tag": "чужой", "selector": ["server-9"]},
            ],
        },
        "observatory": {
            "subjectSelector": ["server-1", "server-4", "чужой-out"],
            "probeURL": "https://example.com",
        },
        "outbounds": [{"tag": "server-1"}],
    }

    stripped = routing.strip_managed(template, ["client-balancer-1"], ["server-1", "server-4"])

    rules = stripped["routing"]["rules"]
    assert [r.get("balancerTag") for r in rules] == [None, None]
    assert stripped["routing"]["balancers"] == [{"tag": "чужой", "selector": ["server-9"]}]
    assert stripped["observatory"]["subjectSelector"] == ["чужой-out"]
    assert "outbounds" in stripped  # всё прочее не тронуто
    assert specs  # подпись аргумента, чтобы тест читался как документация


def test_strip_managed_drops_own_observatory_section() -> None:
    template = {
        "routing": {"rules": []},
        "observatory": {"subjectSelector": ["server-1"], "probeURL": "https://example.com"},
    }

    stripped = routing.strip_managed(template, [], ["server-1"])

    assert "observatory" not in stripped


# ------------------------------------------------------------------ reset


def test_reset_without_yes_only_shows_plan(panel, tmp_path) -> None:
    path = write_config_file(panel, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    sync_direct(make_config(panel.base_url, tmp_path), panel)
    before_template = json.dumps(panel.template, sort_keys=True)
    store = StateStore(str(tmp_path / "state.db"))
    before_assignments = {cid: a.balancer_tag for cid, a in store.load_assignments().items()}
    store.close()

    assert main(["--config", str(path), "reset"]) == 0

    assert json.dumps(panel.template, sort_keys=True) == before_template
    assert panel.write_count == 1
    store = StateStore(str(tmp_path / "state.db"))
    assert {cid: a.balancer_tag for cid, a in store.load_assignments().items()} == before_assignments
    store.close()


def test_reset_rebuilds_state_and_routing_from_scratch(panel, tmp_path) -> None:
    path = write_config_file(panel, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    config = make_config(panel.base_url, tmp_path)
    sync_direct(config, panel)
    assert panel.distribution() == {
        "client-balancer-1": 2,
        "client-balancer-2": 2,
        "client-balancer-3": 2,
    }

    # руками ломаем раскладку: клиент переезжает в другую группу мимо сервиса
    store = StateStore(config.state.database)
    store.apply_changes({1: "client-balancer-3"}, {1: "user1@example"}, [])
    store.close()
    sync_direct(config, panel)
    assert panel.distribution()["client-balancer-3"] == 3
    writes_before = panel.write_count

    assert main(["--config", str(path), "reset", "--yes"]) == 0

    # раскладка снова ровная, а не «как было»
    assert panel.distribution() == {
        "client-balancer-1": 2,
        "client-balancer-2": 2,
        "client-balancer-3": 2,
    }
    assert panel.write_count == writes_before + 1
    store = StateStore(config.state.database)
    assignments = store.load_assignments()
    assert len(assignments) == 6
    assert assignments[1].balancer_tag == "client-balancer-1"
    # `status` после перенастройки должен показывать живые метки, а не «never»
    assert store.get_meta("config_writes_total") == "1"
    assert store.get_meta("last_config_update")
    assert store.get_meta("last_error") == ""
    store.close()


def test_reset_removes_groups_dropped_from_config(panel, tmp_path) -> None:
    """Группа, убранная из config.yaml, исчезает из конфига панели вместе с правилом."""
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    path = write_config_file(panel, tmp_path)
    sync_direct(make_config(panel.base_url, tmp_path), panel)
    assert len(panel.template["routing"]["balancers"]) == 3

    reduced = write_config_file(
        panel,
        tmp_path,
        balancers=[
            {"tag": "client-balancer-1", "primary": "server-1"},
            {"tag": "client-balancer-2", "primary": "server-2"},
        ],
        fallback={"outbound": "server-4"},
    )
    assert reduced == path

    assert main(["--config", str(path), "reset", "--yes"]) == 0

    tags = [b["tag"] for b in panel.template["routing"]["balancers"]]
    assert tags == ["client-balancer-1", "client-balancer-2"]
    assert set(panel.distribution()) == {"client-balancer-1", "client-balancer-2"}
    assert all(tag != "client-balancer-3" for tag in panel.distribution())


def test_reset_clears_protection_counters(panel, tmp_path) -> None:
    """Даже после churn-предохранителя перенастройка проходит (это её и смысл)."""
    path = write_config_file(panel, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    config = make_config(panel.base_url, tmp_path)
    store = StateStore(config.state.database)
    store.set_meta("config_repair_streak", "5")
    store.close()

    assert main(["--config", str(path), "reset", "--yes"]) == 0

    store = StateStore(config.state.database)
    # счётчик churn-предохранителя сброшен вместе с остальным состоянием
    assert store.get_meta("config_repair_streak") is None
    assert store.get_meta("schema_version") == "1"
    store.close()


def test_reset_refuses_when_outbound_missing(panel, tmp_path) -> None:
    """Нет outbound'а — ничего не пишем и состояние не трогаем."""
    path = write_config_file(panel, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 4)])
    panel.template["outbounds"] = [
        ob for ob in panel.template["outbounds"] if ob["tag"] != "server-2"
    ]
    config = make_config(panel.base_url, tmp_path)
    store = StateStore(config.state.database)
    store.apply_changes({1: "client-balancer-1"}, {1: "user1@example"}, [])
    store.close()

    assert main(["--config", str(path), "reset", "--yes"]) == 1

    assert panel.write_count == 0
    store = StateStore(config.state.database)
    assert store.load_assignments()[1].balancer_tag == "client-balancer-1"
    store.close()


def test_reset_keeps_foreign_rules(panel, tmp_path) -> None:
    path = write_config_file(panel, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    manual = {"type": "field", "domain": ["my-rule.example"], "outboundTag": "direct"}
    panel.template["routing"]["rules"].append(dict(manual))
    sync_direct(make_config(panel.base_url, tmp_path), panel)

    assert main(["--config", str(path), "reset", "--yes"]) == 0

    assert manual in panel.template["routing"]["rules"]
