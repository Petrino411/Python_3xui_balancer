"""Тесты точечного переназначения клиентов (`xcb move` / `assign`).

Проверяется главное обещание команды: меняется назначение только указанных клиентов,
конфиг пишется один раз (один sync на все правки), остальные правила не двигаются,
а любые сомнения (нет клиента, неоднозначная подстрока, нет тега, клиент исключён)
приводят к отказу с кодом 2 и без единой записи.
"""

from __future__ import annotations

import pytest

from conftest import make_config
from mock_panel import make_client

from xray_client_balancer.main import build_parser, cmd_clients, cmd_move
from xray_client_balancer.models import Assignment, PanelClient
from xray_client_balancer.ops import (
    MoveError,
    apply_moves,
    check_eligibility,
    find_balancer_tag,
    pick_target,
    plan_moves,
    resolve_client,
)
from xray_client_balancer.service import BalancerService
from xray_client_balancer.database import StateStore


# --------------------------------------------------------------------------- helpers


def _run_move(config, argv: list[str]) -> int:
    """Вызвать команду move так, как это делает CLI (разбор аргументов + функция)."""
    args = build_parser().parse_args(argv)
    return cmd_move(config, args)


def _assignment(store: StateStore, email: str) -> str:
    return next(a.balancer_tag for a in store.load_assignments().values() if a.email == email)


def _client(client_id: int, email: str, **overrides) -> PanelClient:
    return PanelClient(client_id=client_id, email=email, **overrides)


# --------------------------------------------------------------------------- чистые функции


def test_resolve_client_exact_and_substring() -> None:
    clients = [_client(1, "anna@example"), _client(2, "bob@example"), _client(3, "annabel@example")]
    assert resolve_client(clients, "bob@example").client_id == 2
    assert resolve_client(clients, "BOB@example").client_id == 2
    assert resolve_client(clients, "bob").client_id == 2
    assert resolve_client(clients, client_id=3).client_id == 3


def test_resolve_client_refuses_ambiguity_and_unknown() -> None:
    clients = [_client(1, "anna@example"), _client(3, "annabel@example")]
    with pytest.raises(MoveError) as ambiguous:
        resolve_client(clients, "ann")
    assert "подходит к 2" in str(ambiguous.value)

    with pytest.raises(MoveError):
        resolve_client(clients, "nobody@example")
    with pytest.raises(MoveError):
        resolve_client(clients, client_id=99)


def test_find_balancer_tag_accepts_short_and_full_form() -> None:
    tags = ["client-balancer-1", "client-balancer-2", "client-balancer-3"]
    assert find_balancer_tag(tags, "2") == "client-balancer-2"
    assert find_balancer_tag(tags, "client-balancer-3") == "client-balancer-3"
    assert find_balancer_tag(tags, "CLIENT-BALANCER-1") == "client-balancer-1"
    with pytest.raises(MoveError):
        find_balancer_tag(tags, "9")


def test_plan_moves_counts_are_sequential() -> None:
    tags = ["client-balancer-1", "client-balancer-2"]
    assignments = {
        1: Assignment(1, "a@example", tags[0], 0, 0),
        2: Assignment(2, "b@example", tags[0], 0, 0),
        3: Assignment(3, "c@example", tags[1], 0, 0),
    }
    clients = [_client(1, "a@example"), _client(2, "b@example")]
    plans = plan_moves(assignments, clients, {1: tags[1], 2: tags[1]}, tags)
    assert plans[0].counts_before == {tags[0]: 2, tags[1]: 1}
    assert plans[0].counts_after == {tags[0]: 1, tags[1]: 2}
    # второй клиент из той же группы: считается уже с учётом первого переноса
    assert plans[1].counts_after == {tags[0]: 0, tags[1]: 3}
    assert plans[-1].counts_after == {tags[0]: 0, tags[1]: 3}


def test_pick_target_avoids_current_group() -> None:
    tags = ["client-balancer-1", "client-balancer-2", "client-balancer-3"]
    assignments = {
        1: Assignment(1, "a@example", tags[0], 0, 0),
        2: Assignment(2, "b@example", tags[0], 0, 0),
        3: Assignment(3, "c@example", tags[1], 0, 0),
    }
    assert pick_target(assignments, _client(1, "a@example"), tags) == tags[2]


def test_check_eligibility_refuses_excluded_client(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path, exclude_clients=["anna@example"])
    with pytest.raises(MoveError) as refused:
        check_eligibility(config, _client(1, "anna@example"))
    assert refused.value.exit_code == 2


def test_check_eligibility_warns_about_inactive_client(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path, include_disabled=False)
    warnings = check_eligibility(config, _client(1, "off@example", enable=False))
    assert warnings and "include_disabled=false" in warnings[0]


# --------------------------------------------------------------------------- CLI: план и отказ


def test_move_plan_does_not_touch_anything(panel, tmp_path) -> None:
    from xray_client_balancer.service import BalancerService

    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        writes_before = panel.write_count
        code = _run_move(config, ["move", "user1@example", "client-balancer-2"])
        assert code == 0
        assert panel.write_count == writes_before
        assert _assignment(store, "user1@example") != "client-balancer-2"
    finally:
        api.close()
        store.close()


def test_move_applies_and_reports_reverse_command(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        before = _assignment(store, "user1@example")
        target = "client-balancer-2" if before != "client-balancer-2" else "client-balancer-1"
        writes_before = panel.write_count

        assert _run_move(config, ["move", "user1@example", target, "--yes"]) == 0
        assert panel.write_count == writes_before + 1  # один sync на одну правку
        assert _assignment(store, "user1@example") == target
        assert panel.assignment_of("user1@example") == target
        out = capsys.readouterr().out
        assert f"user1@example: {before} -> {target}" in out
        assert f"xcb move user1@example {before} --yes" in out
        assert "hot-apply" in out
    finally:
        api.close()
        store.close()


def test_move_unknown_client_and_tag_exit_2_without_writes(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    assert _run_move(config, ["move", "ghost@example", "client-balancer-2", "--yes"]) == 2
    assert _run_move(config, ["move", "user1@example", "client-balancer-9", "--yes"]) == 2
    assert panel.write_count == 0
    store = StateStore(config.state.database)
    try:
        assert store.load_assignments() == {}
    finally:
        store.close()


def test_move_ambiguous_email_exit_2(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "anna@example"), make_client(2, "annabel@example"))
    assert _run_move(config, ["move", "ann", "client-balancer-2", "--yes"]) == 2
    assert panel.write_count == 0


def test_move_wrong_usage_exit_2(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    # ни тега, ни --to/--auto
    assert _run_move(config, ["move", "user1@example", "--yes"]) == 2
    # --id и позиционный клиент одновременно
    assert _run_move(config, ["move", "--id", "1", "user1@example", "--to", "2", "--yes"]) == 2
    # --auto вместе с --to
    assert _run_move(config, ["move", "user1@example", "--auto", "--to", "2", "--yes"]) == 2


def test_move_already_there_is_noop(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        current = _assignment(store, "user1@example")
        writes_before = panel.write_count
        assert _run_move(config, ["move", "user1@example", current, "--yes"]) == 0
        assert panel.write_count == writes_before
        assert "уже" in capsys.readouterr().out
    finally:
        api.close()
        store.close()


def test_move_several_clients_makes_one_config_write(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        writes_before = panel.write_count
        code = _run_move(
            config,
            ["move", "--to", "client-balancer-3", "user1@example", "user2@example", "--yes"],
        )
        assert code == 0
        assert panel.write_count == writes_before + 1
        assert _assignment(store, "user1@example") == "client-balancer-3"
        assert _assignment(store, "user2@example") == "client-balancer-3"
    finally:
        api.close()
        store.close()


def test_move_by_id_and_auto(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 7)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        assert _run_move(config, ["move", "--id", "1", "--to", "2", "--yes"]) == 0
        assert _assignment(store, "user1@example") == "client-balancer-2"

        # --auto уводит из текущей группы в самую свободную
        current = _assignment(store, "user2@example")
        assert _run_move(config, ["move", "user2@example", "--auto", "--yes"]) == 0
        assert _assignment(store, "user2@example") != current
    finally:
        api.close()
        store.close()


def test_move_new_client_without_assignment(panel, tmp_path) -> None:
    """Клиент есть в панели, но sticky-назначения у него ещё нет — это не отказ."""
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        panel.add_clients(make_client(2, "fresh@example"))
        assert _run_move(config, ["move", "fresh@example", "client-balancer-3", "--yes"]) == 0
        assert _assignment(store, "fresh@example") == "client-balancer-3"
        assert panel.assignment_of("fresh@example") == "client-balancer-3"
    finally:
        api.close()
        store.close()


def test_move_refuses_excluded_client(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path, exclude_clients=["hidden@example"])
    panel.add_clients(make_client(1, "hidden@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        assert _run_move(config, ["move", "hidden@example", "client-balancer-2", "--yes"]) == 2
        assert store.load_assignments() == {}
    finally:
        api.close()
        store.close()


def test_move_keeps_other_clients_in_place(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 10)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        before = {a.email: a.balancer_tag for a in store.load_assignments().values()}
        assert _run_move(config, ["move", "user5@example", "client-balancer-1", "--yes"]) == 0
        after = {a.email: a.balancer_tag for a in store.load_assignments().values()}
        changed = {email for email in before if before[email] != after[email]}
        assert changed == {"user5@example"}
        assert panel.assignment_of("user5@example") == "client-balancer-1"
    finally:
        api.close()
        store.close()


def test_move_panel_unavailable_is_error_not_refusal(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    panel.mode = "unavailable"
    # 1 = «сломалось», а не «отказано в операции»
    assert _run_move(config, ["move", "user1@example", "client-balancer-2", "--yes"]) == 1


# --------------------------------------------------------------------------- применение напрямую


def test_apply_moves_verifies_route_and_reports_hot_apply(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 4)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        service = BalancerService(config, store, api)
        service.sync()
        assignments = store.load_assignments()
        clients = [c for c in service.fetch_clients() if c.email == "user1@example"]
        plans = plan_moves(assignments, clients, {clients[0].client_id: "client-balancer-3"}, service.balancer_tags)
        outcome = apply_moves(service, plans, verify=True)
        assert outcome.db_changed and outcome.config_written
        assert outcome.errors == []
        assert outcome.route_checks and outcome.route_checks[0][1] is True
        assert outcome.core_restarted is False  # правка только routing — hot-apply
        assert "client-balancer-3" in outcome.render()
    finally:
        api.close()
        store.close()


def test_apply_moves_without_changes_writes_nothing(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        service = BalancerService(config, store, api)
        service.sync()
        clients = service.fetch_clients()
        current = store.load_assignments()[clients[0].client_id].balancer_tag
        plans = plan_moves(store.load_assignments(), clients, {clients[0].client_id: current}, service.balancer_tags)
        writes_before = panel.write_count
        outcome = apply_moves(service, plans)
        assert outcome.moved == []
        assert panel.write_count == writes_before
        assert outcome.warnings
    finally:
        api.close()
        store.close()


def test_move_auto_spreads_several_clients(panel, tmp_path) -> None:
    """--auto для нескольких клиентов не должен сваливать их в одну «пустую» группу."""
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 4)])
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        before = {a.email: a.balancer_tag for a in store.load_assignments().values()}
        assert _run_move(config, ["move", "user1@example", "--auto", "--yes"]) == 0
        assert _run_move(config, ["move", "user2@example", "--auto", "--yes"]) == 0
        after = {a.email: a.balancer_tag for a in store.load_assignments().values()}
        assert after["user1@example"] != after["user2@example"], after
        # --auto трогает только указанных клиентов
        assert after["user3@example"] == before["user3@example"]
    finally:
        api.close()
        store.close()


def test_move_same_client_twice_counts_once(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "anna@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
        code = _run_move(
            config, ["move", "--to", "client-balancer-2", "anna@example", "ANNA@example", "--yes"]
        )
        assert code == 0
        out = capsys.readouterr().out
        # клиент, названный дважды (email и подстрокой-дублем), попал в план один раз
        assert out.count("-> client-balancer-2") == 2  # строка плана и строка результата
        assert out.count("xcb move anna@example") == 1
    finally:
        api.close()
        store.close()


def test_clients_command_lists_ids_and_groups(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(7, "seven@example"), make_client(8, "eight@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
    finally:
        api.close()
        store.close()

    args = build_parser().parse_args(["clients"])
    assert cmd_clients(config, args) == 0
    out = capsys.readouterr().out
    assert "seven@example" in out and "eight@example" in out
    assert "нет в панели" not in out

    args = build_parser().parse_args(["clients", "--filter", "seven"])
    assert cmd_clients(config, args) == 0
    out = capsys.readouterr().out
    assert "seven@example" in out and "eight@example" not in out


def test_clients_command_offline_works_without_panel(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    panel.add_clients(make_client(1, "user1@example"))
    store = StateStore(config.state.database)
    from xray_client_balancer.api import PanelApi

    api = PanelApi(config.panel)
    try:
        BalancerService(config, store, api).sync()
    finally:
        api.close()
        store.close()
    panel.stop()  # панель больше не отвечает

    args = build_parser().parse_args(["clients"])
    assert cmd_clients(config, args) == 0
    assert "ВНИМАНИЕ" in capsys.readouterr().out

    args = build_parser().parse_args(["clients", "--offline"])
    assert cmd_clients(config, args) == 0
    assert "--offline" in capsys.readouterr().out
