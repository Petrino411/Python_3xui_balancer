"""Защиты §10/§33/§37: откат, если ядро не поднялось, и routeTest с inboundTag."""

from __future__ import annotations

import json

from mock_panel import make_client


def sync(service, **kwargs):
    return service.sync(**kwargs)


def managed_users(template: dict, balancer_tag: str) -> list[str]:
    """Пользователи, привязанные к балансировщику в конфиге."""
    for rule in (template.get("routing") or {}).get("rules") or []:
        if rule.get("balancerTag") == balancer_tag:
            return list(rule.get("user") or [])
    return []


def test_rollback_when_core_fails_to_start(service, panel) -> None:
    """Если после записи ядро не поднялось — конфиг возвращается к предыдущему."""
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    service.config.safety.xray_ready_timeout = 1.0

    # «ломается» любой конфиг, в котором появились наши балансировщики
    panel.core_fails_for = lambda template: bool(
        (template.get("routing") or {}).get("balancers")
    )

    report = sync(service)

    assert report.errors, "ожидалась ошибка о неподнявшемся ядре"
    assert any("не поднялось" in e for e in report.errors)
    # записей было две: кандидат + откат
    assert panel.write_count == 2
    # в панели снова исходный конфиг: наших балансировщиков нет
    assert not (panel.template.get("routing") or {}).get("balancers")
    # откат вернул и правила
    assert managed_users(panel.template, ["client-balancer-1"]) == []


def test_no_rollback_when_core_is_running(service, panel) -> None:
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    service.config.safety.xray_ready_timeout = 1.0

    report = sync(service)

    assert report.errors == []
    assert panel.write_count == 1
    assert (panel.template.get("routing") or {}).get("balancers")


def test_route_test_uses_inbound_tags(service, panel) -> None:
    """Правило может совпадать по inboundTag: без него ядро отвечает matched=false."""
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    panel.running_inbounds = [{"tag": "in-44749-tcp", "port": 44749, "protocol": "vless"}]

    seen_inbound_tags: list[str] = []
    original = panel.route_test

    def spy(form):
        seen_inbound_tags.append(form.get("inboundTag", ""))
        return original(form)

    panel.route_override = spy

    report = sync(service)

    assert report.errors == []
    # сервис должен был спросить ядро и без inboundTag, и с тегами inbound'ов
    assert "" in seen_inbound_tags
    assert "in-44749-tcp" in seen_inbound_tags


def test_route_test_problem_is_reported(service, panel) -> None:
    """§43: если ядро возвращает неожиданный outbound — это критично и видно в отчёте."""
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    panel.route_override = lambda form: {"matched": False, "outboundTag": "", "groupTags": []}

    report = sync(service)

    assert any("должен идти через" in e for e in report.route_check_errors)
    assert report.route_check_errors, "ошибки проверки маршрутов должны быть в отчёте (§43)"


CORE_STARTING = (
    "Something went wrong (rpc error: code = Unavailable desc = connection error: "
    'desc = "transport: Error while dialing: dial tcp 127.0.0.1:62789: connect: connection refused")'
)


def test_core_starting_is_warning_not_route_error(service, panel) -> None:
    """§37: ядро перезапускается после записи — grpc-api молчит. Это не ошибка маршрутизации."""
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    panel.route_failure = CORE_STARTING

    report = sync(service)

    assert report.config_written is True
    assert report.errors == [], "стартующее ядро не должно попадать в ошибки"
    assert report.route_check_errors == []
    assert panel.write_count == 1, "откат при этом не нужен"


def test_route_unavailable_with_core_down_is_reported(service, panel) -> None:
    """Если ядро вообще не работает — молча пропускать проверку нельзя."""
    panel.add_clients(*[make_client(i) for i in range(1, 4)])
    panel.route_failure = CORE_STARTING
    panel.xray_state = "error"  # панель: ядро не работает

    problems = service._verify_routes({"client-balancer-1": ["client-1"]})

    assert problems, "недоступный routeTest при мёртвом ядре должен попасть в отчёт"
    assert "routeTest недоступен" in problems[0]
