"""Тесты распределения (§41): 3/4/10 клиентов, добавление, удаление, детерминизм."""

from __future__ import annotations

from xray_client_balancer.allocator import (
    assign_new_clients,
    build_plan,
    distributed_evenly,
    rebalance_assignments,
    rebalance_diff,
)
from xray_client_balancer.models import Assignment, BalancerSpec, PanelClient

SPECS = [
    BalancerSpec(tag="client-balancer-1", primary="server-1", fallback="server-4"),
    BalancerSpec(tag="client-balancer-2", primary="server-2", fallback="server-4"),
    BalancerSpec(tag="client-balancer-3", primary="server-3", fallback="server-4"),
]


def clients(count: int) -> list[PanelClient]:
    return [PanelClient(client_id=i, email=f"user{i}@example") for i in range(1, count + 1)]


def assignments_from(target: dict[int, str]) -> dict[int, Assignment]:
    return {
        cid: Assignment(client_id=cid, email=f"user{cid}@example", balancer_tag=tag, created_at=0, updated_at=0)
        for cid, tag in target.items()
    }


def counts(values: dict[int, str]) -> list[int]:
    result = [0, 0, 0]
    for index, spec in enumerate(SPECS):
        result[index] = list(values.values()).count(spec.tag)
    return result


def test_three_clients() -> None:
    result = assign_new_clients({}, clients(3), SPECS)
    assert counts(result) == [1, 1, 1]


def test_four_clients() -> None:
    result = assign_new_clients({}, clients(4), SPECS)
    assert counts(result) == [2, 1, 1]


def test_ten_clients() -> None:
    result = assign_new_clients({}, clients(10), SPECS)
    assert counts(result) == [4, 3, 3]


def test_eleven_clients_first_run() -> None:
    """§19: 11 клиентов на пустой БД -> 4/4/3, порядок детерминирован."""
    result = assign_new_clients({}, clients(11), SPECS)
    assert counts(result) == [4, 4, 3]
    assert result[1] == "client-balancer-1"
    assert result[2] == "client-balancer-2"
    assert result[3] == "client-balancer-3"
    assert result[4] == "client-balancer-1"


def test_deterministic_regardless_of_input_order() -> None:
    a = assign_new_clients({}, clients(10), SPECS)
    b = assign_new_clients({}, list(reversed(clients(10))), SPECS)
    assert a == b


def test_new_client_goes_to_smallest_group() -> None:
    """§3/§4: было 4/3/3, пришёл новый -> 4/4/3, старые не двигаются."""
    existing = {
        cid: Assignment(
            client_id=cid, email=f"user{cid}@example", balancer_tag=tag, created_at=0, updated_at=0
        )
        for cid, tag in assign_new_clients({}, clients(10), SPECS).items()
    }
    before = dict(existing)
    current = clients(11)
    new = assign_new_clients(existing, current, SPECS)
    assert new == {11: "client-balancer-2"}
    merged = {**{cid: a.balancer_tag for cid, a in before.items()}, **new}
    assert counts(merged) == [4, 4, 3]
    # существующие назначения не изменились
    assert all(merged[cid] == a.balancer_tag for cid, a in before.items())


def test_deletion_does_not_trigger_redistribution() -> None:
    """§5: удалили двух из B1 -> 2/4/4, остальные не переезжают."""
    existing = {
        cid: Assignment(
            client_id=cid, email=f"user{cid}@example", balancer_tag="client-balancer-1",
            created_at=0, updated_at=0,
        )
        for cid in (1, 2, 3, 4)
    }
    existing.update(
        {
            cid: Assignment(
                client_id=cid, email=f"user{cid}@example", balancer_tag="client-balancer-2",
                created_at=0, updated_at=0,
            )
            for cid in (5, 6, 7, 8)
        }
    )
    existing.update(
        {
            cid: Assignment(
                client_id=cid, email=f"user{cid}@example", balancer_tag="client-balancer-3",
                created_at=0, updated_at=0,
            )
            for cid in (9, 10, 11, 12)
        }
    )
    remaining = [c for c in clients(12) if c.client_id not in (3, 4)]
    plan = build_plan(existing, remaining, SPECS)
    assert sorted(plan.removed_client_ids) == [3, 4]
    assert plan.new_assignments == {}
    assert {tag: len(emails) for tag, emails in plan.groups.items()} == {
        "client-balancer-1": 2,
        "client-balancer-2": 4,
        "client-balancer-3": 4,
    }


def test_disabled_client_keeps_assignment() -> None:
    """§27: disabled клиент остаётся в БД и в правилах."""
    existing = assignments_from({1: "client-balancer-1"})
    plan = build_plan(
        existing,
        [PanelClient(client_id=1, email="user1@example", enable=False)],
        SPECS,
    )
    assert plan.removed_client_ids == []
    assert plan.new_assignments == {}
    assert plan.groups["client-balancer-1"] == ["user1@example"]


def test_client_in_two_inbounds_gets_one_assignment() -> None:
    """§26: клиент, присутствующий в нескольких inbound, имеет одно назначение."""
    existing = assignments_from({7: "client-balancer-2"})
    plan = build_plan(
        existing,
        [PanelClient(client_id=7, email="multi@example", inbound_ids=(2, 5, 9))],
        SPECS,
    )
    assert plan.new_assignments == {}
    assert plan.removed_client_ids == []
    assert plan.groups["client-balancer-2"] == ["multi@example"]
    assert sum(len(v) for v in plan.groups.values()) == 1


def test_duplicate_emails_are_deduplicated_in_rule() -> None:
    """Две записи с одинаковым email не должны дать два одинаковых user в правиле."""
    existing = assignments_from({7: "client-balancer-2", 8: "client-balancer-3"})
    plan = build_plan(
        existing,
        [
            PanelClient(client_id=7, email="dup@example", inbound_ids=(2,)),
            PanelClient(client_id=8, email="dup@example", inbound_ids=(3,)),
        ],
        SPECS,
        include_disabled=True,
    )
    assert plan.groups["client-balancer-2"] == ["dup@example"]
    assert plan.groups["client-balancer-3"] == ["dup@example"]


def test_excluded_clients_are_not_routed() -> None:
    plan = build_plan(
        {},
        [PanelClient(client_id=1, email="admin@example"), PanelClient(client_id=2, email="user2@example")],
        SPECS,
        is_excluded=lambda email: email == "admin@example",
    )
    assert plan.excluded_emails == ["admin@example"]
    assert plan.groups["client-balancer-1"] == ["user2@example"]
    assert sum(len(v) for v in plan.groups.values()) == 1


def test_rebalance_evens_out() -> None:
    """§21: 20/50/50 -> ровные группы."""
    existing: dict[int, Assignment] = {}
    for cid in range(1, 121):
        tag = (
            "client-balancer-1"
            if cid <= 20
            else "client-balancer-2"
            if cid <= 70
            else "client-balancer-3"
        )
        existing[cid] = Assignment(
            client_id=cid, email=f"user{cid}@example", balancer_tag=tag, created_at=0, updated_at=0
        )
    current = [PanelClient(client_id=cid, email=f"user{cid}@example") for cid in range(1, 121)]
    target = rebalance_assignments(current, SPECS)
    counts_target = [list(target.values()).count(spec.tag) for spec in SPECS]
    assert counts_target == [40, 40, 40]
    changed = rebalance_diff(existing, target)
    assert len(changed) == 80


def test_evenness_helper() -> None:
    assert distributed_evenly({"a": 4, "b": 3, "c": 3})
    assert distributed_evenly({"a": 2, "b": 4, "c": 4}) is False
    assert distributed_evenly({"a": 7}) is True
