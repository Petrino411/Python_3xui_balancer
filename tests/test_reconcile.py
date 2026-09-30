"""Синхронизация против mock-панели: новые клиенты, смена email, идемпотентность, ошибки API."""

from __future__ import annotations

import pytest

from mock_panel import make_client

from xray_client_balancer.routing import canonical


def sync(service, **kwargs):
    return service.sync(**kwargs)


def test_first_sync_assigns_evenly_and_writes_once(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 10)])
    report = sync(service)

    assert report.clients_total == 9
    assert panel.distribution() == {
        "client-balancer-1": 3,
        "client-balancer-2": 3,
        "client-balancer-3": 3,
    }
    assert panel.write_count == 1
    assert report.config_written
    # healthcheck создан ровно один раз вместе с балансировщиками
    assert "observatory" in panel.template
    assert panel.template["observatory"]["subjectSelector"] == [
        "server-1",
        "server-2",
        "server-3",
        "server-4",
    ]
    assert len(panel.template["routing"]["balancers"]) == 3
    assert report.errors == []


def test_second_sync_is_idempotent(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 10)])
    sync(service)
    assignments_before = service.store.load_assignments()
    template_after_first = canonical(panel.template)

    report = sync(service)

    assert panel.write_count == 1  # ни одной лишней записи
    assert not report.has_changes
    assert report.config_written is False
    assert canonical(panel.template) == template_after_first
    after = service.store.load_assignments()
    assert {cid: a.updated_at for cid, a in after.items()} == {
        cid: a.updated_at for cid, a in assignments_before.items()
    }
    assert {cid: a.balancer_tag for cid, a in after.items()} == {
        cid: a.balancer_tag for cid, a in assignments_before.items()
    }


def test_new_client_goes_to_smallest_group(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 11)])  # 4/3/3
    sync(service)
    before = {cid: a.balancer_tag for cid, a in service.store.load_assignments().items()}

    panel.add_clients(make_client(11, "new@example"))
    report = sync(service)

    after = service.store.load_assignments()
    # минимальные группы — B2 и B3; tie-break выбирает первую по порядку конфига
    assert after[11].balancer_tag == "client-balancer-2"
    assert panel.distribution() == {
        "client-balancer-1": 4,
        "client-balancer-2": 4,
        "client-balancer-3": 3,
    }
    # старые назначения не изменились
    assert all(after[cid].balancer_tag == tag for cid, tag in before.items())
    assert panel.assignment_of("new@example") == after[11].balancer_tag
    assert panel.write_count == 2


def test_email_rename_keeps_assignment(service, panel) -> None:
    """§6: сменился email у того же внутреннего id — назначение сохраняется."""
    panel.add_clients(make_client(1, "old@example"))
    sync(service)
    assigned = service.store.load_assignments()[1].balancer_tag

    panel.clients = [make_client(1, "new@example")]
    report = sync(service)

    assignment = service.store.load_assignments()[1]
    assert assignment.balancer_tag == assigned
    assert assignment.email == "new@example"
    assert panel.assignment_of("new@example") == assigned
    assert panel.assignment_of("old@example") == ""
    assert report.email_updates == [(1, "old@example", "new@example")]


def test_dry_run_changes_nothing(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 5)])
    report = sync(service, dry_run=True)

    assert report.dry_run
    assert report.clients_total == 4
    assert len(report.new_assignments) == 4
    assert service.store.load_assignments() == {}
    assert panel.write_count == 0
    assert panel.managed_rules() == []
    rendered = report.render(service.config)
    assert "No changes applied." in rendered
    assert "user1@example -> client-balancer-1" in rendered


def test_api_unavailable_keeps_state(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    sync(service)
    template_before = canonical(panel.template)
    assignments_before = service.store.load_assignments()

    panel.mode = "unavailable"
    report = sync(service)

    assert report.errors
    assert panel.write_count == 1
    assert canonical(panel.template) == template_before
    assert service.store.load_assignments() == assignments_before


def test_empty_client_list_is_not_treated_as_zero(service, panel) -> None:
    """§32: пустой ответ API не должен удалять правила."""
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    sync(service)
    template_before = canonical(panel.template)

    panel.mode = "empty"
    report = sync(service)

    assert report.errors and "пустой список клиентов" in report.errors[0]
    assert canonical(panel.template) == template_before
    assert len(service.store.load_assignments()) == 3
    assert panel.write_count == 1


def test_malformed_client_list_is_rejected(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    sync(service)
    template_before = canonical(panel.template)

    panel.mode = "malformed"
    report = sync(service)

    assert report.errors
    assert canonical(panel.template) == template_before


def test_invalid_candidate_is_not_applied(service, panel) -> None:
    """§10: если нужного outbound нет — конфиг не пишется, но состояние сходится позже."""
    panel.template["outbounds"] = [ob for ob in panel.template["outbounds"] if ob["tag"] != "server-3"]
    panel.add_clients(make_client(1))
    report = sync(service)

    assert report.errors
    assert any("server-3" in err for err in report.errors)
    assert panel.write_count == 0
    # локальное состояние всё же зафиксировано: назначение sticky
    assert service.store.load_assignments()[1].balancer_tag == "client-balancer-1"

    # outbound появился — следующий цикл догоняет конфиг без перераспределения
    panel.template["outbounds"].append({"tag": "server-3", "protocol": "vless"})
    report2 = sync(service)
    assert report2.errors == []
    assert panel.write_count == 1
    assert panel.assignment_of("user1@example") == "client-balancer-1"


def test_excluded_clients_never_in_rules(service, panel) -> None:
    service.config.exclude_clients = ["admin@example"]
    service.config.exclude_regex = ["^test-"]
    panel.add_clients(
        make_client(1, "admin@example"),
        make_client(2, "test-1@example"),
        make_client(3, "user3@example"),
    )
    report = sync(service)

    rules = panel.managed_rules()
    emails = [e for rule in rules for e in rule["user"]]
    assert emails == ["user3@example"]
    assert sorted(report.excluded) == ["admin@example", "test-1@example"]
    assert set(service.store.load_assignments()) == {3}


def test_route_test_failure_is_reported_but_no_loop(service, panel) -> None:
    panel.add_clients(make_client(1))
    panel.route_override = lambda form: {"matched": False, "outboundTag": "", "groupTags": []}

    report = sync(service)

    assert report.route_check_errors
    assert panel.write_count == 1  # конфиг не переписывается бесконечно
    report2 = sync(service)
    assert panel.write_count == 1
    assert report2.config_written is False


def test_managed_rules_are_repaired_after_manual_delete(service, panel) -> None:
    """§25: пользователь удалил одно управляемое правило — сервис его вернёт."""
    panel.add_clients(*[make_client(i) for i in range(1, 7)])
    sync(service)
    rules = panel.template["routing"]["rules"]
    panel.template["routing"]["rules"] = [r for r in rules if r.get("balancerTag") != "client-balancer-2"]

    report = sync(service)

    assert report.config_written
    assert panel.assignment_of("user2@example") == "client-balancer-2"
    assert {r["balancerTag"] for r in panel.managed_rules()} == {
        "client-balancer-1",
        "client-balancer-2",
        "client-balancer-3",
    }


def test_manual_routing_rule_is_preserved(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    manual = {"type": "field", "domain": ["geosite:private"], "outboundTag": "direct"}
    panel.template["routing"]["rules"].insert(1, dict(manual))
    sync(service)

    assert manual in panel.template["routing"]["rules"]
    assert panel.template["routing"]["rules"][0]["outboundTag"] == "api"


def test_backup_created_on_change_only(service, panel, tmp_path) -> None:
    panel.add_clients(make_client(1))
    sync(service)
    backups_after_first = sorted((tmp_path / "backups").glob("*.json"))
    assert len(backups_after_first) == 1

    sync(service)
    assert sorted((tmp_path / "backups").glob("*.json")) == backups_after_first

    panel.add_clients(make_client(2))
    sync(service)
    assert len(sorted((tmp_path / "backups").glob("*.json"))) == 2


def test_backup_rotation_keeps_limit(service, panel, tmp_path) -> None:
    service.config.backups.keep = 2
    service.backups.keep = 2
    for index in range(1, 6):
        panel.add_clients(make_client(index))
        sync(service)
    assert len(sorted((tmp_path / "backups").glob("*.json"))) == 2


def test_loss_of_foreign_change_after_write_is_detected(service, panel, monkeypatch) -> None:
    """§9: если между чтением и записью чужое правило потерялось — сервис это замечает и выравнивает."""
    panel.add_clients(make_client(1))
    original_update = service.api.update_xray_template
    injected = {"done": False}

    def update_with_race(template, outbound_test_url=None):  # type: ignore[no-untyped-def]
        original_update(template, outbound_test_url)
        if not injected["done"]:
            injected["done"] = True
            # «пользователь» одновременно сохранил своё правило из UI
            panel.template["routing"]["rules"].insert(
                1, {"type": "field", "domain": ["my-rule.example"], "outboundTag": "direct"}
            )

    monkeypatch.setattr(service.api, "update_xray_template", update_with_race)
    report = sync(service)

    # чужое правило осталось, наш блок выровнен, паники нет
    domains = [r.get("domain") for r in panel.template["routing"]["rules"] if r.get("domain")]
    assert ["my-rule.example"] in domains
    assert panel.assignment_of("user1@example") == "client-balancer-1"
    assert report.errors == []
    assert report.repair_events == 1


def test_churn_breaker_stops_writes(service, panel) -> None:
    service.config.safety.churn_breaker_cycles = 2
    service.store.set_meta("config_repair_streak", "2")
    panel.add_clients(make_client(1))

    report = sync(service)

    assert report.errors and "churn" in report.errors[0]
    assert panel.write_count == 0

    # руками с --force-write запись разрешена
    report2 = sync(service, force_write=True)
    assert report2.config_written
    assert panel.write_count == 1


def test_status_meta_is_updated(service, panel) -> None:
    panel.add_clients(make_client(1))
    sync(service)
    meta = service.store.meta()
    assert meta["last_successful_sync"]
    assert meta["last_config_update"]
    assert meta["config_writes_total"] == "1"
