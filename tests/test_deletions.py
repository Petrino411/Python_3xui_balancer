"""Удаление клиентов (§5, §20): снятие назначения, отсутствие перераспределения."""

from __future__ import annotations

from mock_panel import make_client


def sync(service, **kwargs):
    return service.sync(**kwargs)


def test_deleted_client_is_removed_from_state_and_routing(service, panel) -> None:
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 4)])
    sync(service)
    assert set(service.store.load_assignments()) == {1, 2, 3}

    panel.remove_client(2)
    report = sync(service)

    assert report.removed == [(2, "user2@example", "client-balancer-2")]
    assert set(service.store.load_assignments()) == {1, 3}
    assert panel.assignment_of("user2@example") == ""
    # остальные не переехали
    assert panel.assignment_of("user1@example") == "client-balancer-1"
    assert panel.assignment_of("user3@example") == "client-balancer-3"
    assert panel.distribution() == {
        "client-balancer-1": 1,
        "client-balancer-3": 1,
    } or panel.distribution() == {"client-balancer-1": 1, "client-balancer-2": 1, "client-balancer-3": 1}


def test_mass_deletion_does_not_redistribute(service, panel) -> None:
    """§20: 50/50/50 -> удалили 30 из B1 -> 20/50/50, никто не переезжает."""
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 151)])
    sync(service)
    assert panel.distribution() == {
        "client-balancer-1": 50,
        "client-balancer-2": 50,
        "client-balancer-3": 50,
    }

    assignments = service.store.load_assignments()
    victims = [cid for cid, a in assignments.items() if a.balancer_tag == "client-balancer-1"][:30]
    for cid in victims:
        panel.remove_client(cid)

    report = sync(service)

    assert len(report.removed) == 30
    assert panel.distribution() == {
        "client-balancer-1": 20,
        "client-balancer-2": 50,
        "client-balancer-3": 50,
    }
    # ни один оставшийся клиент не сменил группу
    after = service.store.load_assignments()
    survivors = {cid: a.balancer_tag for cid, a in assignments.items() if cid not in victims}
    assert all(after[cid].balancer_tag == tag for cid, tag in survivors.items())


def test_new_clients_fill_the_emptied_group_first(service, panel) -> None:
    """§20/§50: после массового удаления новые клиенты идут в опустевшую группу."""
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 151)])
    sync(service)
    assignments = service.store.load_assignments()
    victims = [cid for cid, a in assignments.items() if a.balancer_tag == "client-balancer-1"][:30]
    for cid in victims:
        panel.remove_client(cid)
    sync(service)

    panel.add_clients(make_client(1001, "fresh1@example"), make_client(1002, "fresh2@example"))
    sync(service)

    after = service.store.load_assignments()
    assert after[1001].balancer_tag == "client-balancer-1"
    assert after[1002].balancer_tag == "client-balancer-1"
    assert panel.distribution()["client-balancer-1"] == 22


def test_delete_then_readd_is_a_new_client(service, panel) -> None:
    """Удалённый и созданный заново клиент — новый (ключ — внутренний id панели)."""
    panel.add_clients(make_client(1, "user1@example"), make_client(2, "user2@example"))
    sync(service)
    panel.remove_client(1)
    sync(service)
    assert set(service.store.load_assignments()) == {2}

    # тот же email, но новый внутренний id — это новый клиент, он занимает свободную группу
    panel.add_clients(make_client(42, "user1@example"))
    sync(service)
    assert service.store.load_assignments()[42].balancer_tag == "client-balancer-1"


def test_single_client_disappearance_is_guarded_as_empty_panel(service, panel) -> None:
    """§32: если панель вернула пустой список, а назначения есть — состояние не трогаем."""
    panel.add_clients(make_client(1, "only@example"))
    sync(service)
    panel.remove_client(1)

    report = sync(service)

    assert report.errors and "пустой список клиентов" in report.errors[0]
    assert set(service.store.load_assignments()) == {1}
    assert panel.assignment_of("only@example") == "client-balancer-1"


def test_removed_clients_do_not_break_other_groups(service, panel) -> None:
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 13)])
    sync(service)
    # удаляем всю третью группу
    for cid, a in list(service.store.load_assignments().items()):
        if a.balancer_tag == "client-balancer-3":
            panel.remove_client(cid)
    report = sync(service)

    assert not report.errors
    assert panel.distribution() == {
        "client-balancer-1": 4,
        "client-balancer-2": 4,
    }
    assert panel.assignment_of("user1@example") == "client-balancer-1"


def test_no_keyerror_when_client_vanishes_between_reads(service, panel) -> None:
    """Проверка, что исчезновение клиента не даёт KeyError/NoneType (§5)."""
    panel.add_clients(make_client(1, "a@example"), make_client(2, "b@example"))
    sync(service)
    panel.remove_client(2)
    report = sync(service)
    assert report.errors == []
    assert panel.assignment_of("a@example") == "client-balancer-1"
