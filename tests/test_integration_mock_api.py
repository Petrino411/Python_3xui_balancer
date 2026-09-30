"""Интеграционный сценарий §42 на mock-панели 3x-ui.

1. API возвращает 9 клиентов      -> 3/3/3
2. API возвращает 10 клиентов     -> 4/3/3
3. удалить клиента из B2          -> 4/2/3
4. добавить двух                  -> новые идут в B2, старые назначения не меняются
"""

from __future__ import annotations

from mock_panel import make_client


def distribution(service) -> dict[str, int]:
    counts = {tag: 0 for tag in service.balancer_tags}
    for assignment in service.store.load_assignments().values():
        counts[assignment.balancer_tag] += 1
    return counts


def test_full_scenario(service, panel) -> None:
    # 1-2. 9 клиентов
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 10)])
    service.sync()
    assert distribution(service) == {
        "client-balancer-1": 3,
        "client-balancer-2": 3,
        "client-balancer-3": 3,
    }
    assert panel.distribution() == {
        "client-balancer-1": 3,
        "client-balancer-2": 3,
        "client-balancer-3": 3,
    }

    # 3. десятый клиент -> выравнивается минимальная группа (B1), детерминированный tie-break
    panel.add_clients(make_client(10, "user10@example"))
    service.sync()
    assert distribution(service) == {
        "client-balancer-1": 4,
        "client-balancer-2": 3,
        "client-balancer-3": 3,
    }

    # 4. удалить клиента из B2 -> 4/2/3
    victim = next(cid for cid, a in service.store.load_assignments().items() if a.balancer_tag == "client-balancer-2")
    panel.remove_client(victim)
    service.sync()
    assert distribution(service) == {
        "client-balancer-1": 4,
        "client-balancer-2": 2,
        "client-balancer-3": 3,
    }

    # 5. добавить двух: оба идут в освободившуюся B2
    before = {cid: a.balancer_tag for cid, a in service.store.load_assignments().items()}
    panel.add_clients(make_client(101, "new1@example"), make_client(102, "new2@example"))
    service.sync()
    after = service.store.load_assignments()
    assert after[101].balancer_tag == "client-balancer-2"
    assert after[102].balancer_tag == "client-balancer-2"
    assert panel.distribution() == {
        "client-balancer-1": 4,
        "client-balancer-2": 4,
        "client-balancer-3": 3,
    }
    # старые назначения не изменились
    assert all(after[cid].balancer_tag == tag for cid, tag in before.items())

    # 6. конфиг написан ровно при каждом фактическом изменении, и не больше
    assert panel.write_count == 4

    # 7. повторный sync ничего не делает
    report = service.sync()
    assert not report.has_changes
    assert panel.write_count == 4


def test_restart_preserves_assignments(service, panel, tmp_path) -> None:
    """§17: перезапуск сервиса не меняет распределение."""
    from xray_client_balancer.api import PanelApi
    from xray_client_balancer.database import StateStore
    from xray_client_balancer.service import BalancerService

    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 11)])
    service.sync()
    before = {
        cid: (a.email, a.balancer_tag, a.created_at) for cid, a in service.store.load_assignments().items()
    }
    writes = panel.write_count

    # «перезапуск»: новый StateStore и новый сервис на той же БД
    store = StateStore(service.config.state.database)
    api = PanelApi(service.config.panel)
    restarted = BalancerService(service.config, store, api)
    report = restarted.sync()

    after = {cid: (a.email, a.balancer_tag, a.created_at) for cid, a in store.load_assignments().items()}
    assert after == before
    assert not report.has_changes
    assert panel.write_count == writes
    api.close()
    store.close()


def test_client_in_several_inbounds_single_rule_entry(service, panel) -> None:
    panel.add_clients(
        make_client(1, "multi@example", inboundIds=[2, 5, 9]),
        make_client(2, "other@example", inboundIds=[2]),
    )
    service.sync()
    emails = [e for rule in panel.managed_rules() for e in rule["user"]]
    assert emails.count("multi@example") == 1
    assert len(service.store.load_assignments()) == 2


def test_route_test_verifies_groups(service, panel) -> None:
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    report = service.sync()
    assert report.route_check_errors == []
    for tag, primary in (
        ("client-balancer-1", "server-1"),
        ("client-balancer-2", "server-2"),
        ("client-balancer-3", "server-3"),
    ):
        email = next(rule["user"][0] for rule in panel.managed_rules() if rule["balancerTag"] == tag)
        result = service.api.route_test(domain="example.com", email=email)
        assert result.matched
        assert result.group_tags == [tag]
        assert result.outbound_tag == primary
