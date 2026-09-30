"""Тесты управления routing: свой блок, чужие правила, позиция, балансировщики, healthcheck."""

from __future__ import annotations

import copy

from xray_client_balancer.config import ObservatoryConfigModel, RoutingConfig
from xray_client_balancer.models import BalancerSpec
from xray_client_balancer.routing import (
    MANAGED_COMMENT_PREFIX,
    apply_balancers,
    apply_managed_block,
    apply_observatory,
    build_managed_rule,
    canonical,
    describe_managed_block,
    find_position_indices,
    foreign_diff,
    foreign_signature,
    is_managed_rule,
    managed_block,
    observatory_requirement,
    template_changed,
    validate_candidate,
)

SPECS = [
    BalancerSpec(tag="client-balancer-1", primary="server-1", fallback="server-4"),
    BalancerSpec(tag="client-balancer-2", primary="server-2", fallback="server-4"),
    BalancerSpec(tag="client-balancer-3", primary="server-3", fallback="server-4"),
]
TAGS = [spec.tag for spec in SPECS]

ROUTING = RoutingConfig()


def template() -> dict:
    return {
        "log": {"loglevel": "warning"},
        "outbounds": [{"tag": f"server-{i}", "protocol": "vless"} for i in (1, 2, 3, 4)],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "domain": ["geosite:category-ads"], "outboundTag": "blocked"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
            ],
        },
    }


GROUPS = {
    "client-balancer-1": ["a@example", "d@example"],
    "client-balancer-2": ["b@example"],
    "client-balancer-3": [],
}


def test_managed_rule_shape() -> None:
    rule = build_managed_rule("client-balancer-1", ["b@example", "a@example"], ROUTING)
    assert rule["user"] == ["a@example", "b@example"]  # §28 стабильный порядок
    assert rule["balancerTag"] == "client-balancer-1"
    assert rule["type"] == "field"
    assert rule["comment"] == f"{MANAGED_COMMENT_PREFIX}client-balancer-1"
    assert rule["enabled"] is True
    assert rule["ruleTag"] == "xcb-rule:client-balancer-1"


def test_empty_group_produces_no_rule() -> None:
    """Пустой user[] у Xray не значит «никого» — пустых правил быть не должно."""
    block = managed_block(GROUPS, SPECS, ROUTING)
    assert [r["balancerTag"] for r in block] == ["client-balancer-1", "client-balancer-2"]
    assert all(r["user"] for r in block)


def test_block_appended_at_bottom_by_default() -> None:
    before = template()
    after = apply_managed_block(before, managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    rules = after["routing"]["rules"]
    assert len(rules) == 5
    assert rules[-1]["balancerTag"] == "client-balancer-2"
    # пользовательские правила сохранены и не переставлены
    assert rules[:3] == before["routing"]["rules"]


def test_block_at_top_when_configured() -> None:
    routing = RoutingConfig(managed_position="top")
    after = apply_managed_block(template(), managed_block(GROUPS, SPECS, routing), routing, TAGS)
    assert after["routing"]["rules"][0]["balancerTag"] == "client-balancer-1"


def test_block_after_rule_marker() -> None:
    routing = RoutingConfig(managed_position="after_rule", insert_after_rule="api")
    after = apply_managed_block(template(), managed_block(GROUPS, SPECS, routing), routing, TAGS)
    rules = after["routing"]["rules"]
    assert rules[0]["outboundTag"] == "api"
    assert rules[1]["balancerTag"] == "client-balancer-1"
    assert rules[3]["outboundTag"] == "blocked"


def test_existing_position_is_kept() -> None:
    base = apply_managed_block(template(), managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    indices_before = find_position_indices(base["routing"]["rules"], TAGS)
    grown = dict(GROUPS)
    grown["client-balancer-2"] = ["b@example", "c@example"]
    updated = apply_managed_block(base, managed_block(grown, SPECS, ROUTING), ROUTING, TAGS)
    assert find_position_indices(updated["routing"]["rules"], TAGS) == indices_before
    assert updated["routing"]["rules"][-1]["user"] == ["b@example", "c@example"]


def test_foreign_rules_and_sections_untouched() -> None:
    before = template()
    after = apply_managed_block(before, managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    assert foreign_signature(before, TAGS) == foreign_signature(after, TAGS)
    assert after["log"] == before["log"]
    assert after["outbounds"] == before["outbounds"]
    assert after["routing"]["domainStrategy"] == "AsIs"


def test_panel_reordering_is_not_a_lost_update() -> None:
    """Панель поднимает своё api-правило наверх — это перестановка, а не потеря правок."""
    before = template()
    after = apply_managed_block(before, managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    reordered = copy.deepcopy(after)
    rules = reordered["routing"]["rules"]
    api_rule = rules.pop(0)
    rules.append(api_rule)
    processed = copy.deepcopy(reordered)
    processed["routing"]["rules"] = [api_rule, *rules[:-1]]
    assert foreign_signature(processed, TAGS) == foreign_signature(after, TAGS)
    assert foreign_diff(after, processed, TAGS).empty


def test_foreign_change_is_detected() -> None:
    before = template()
    after = apply_managed_block(before, managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    tampered = copy.deepcopy(after)
    tampered["routing"]["rules"].insert(0, {"type": "field", "domain": ["example.net"], "outboundTag": "blocked"})
    diff = foreign_diff(after, tampered, TAGS)
    assert not diff.empty
    assert len(diff.added_rules) == 1


def test_balancers_ours_replaced_foreign_kept() -> None:
    tpl = template()
    tpl["routing"]["balancers"] = [
        {"tag": "user-balancer", "selector": ["server-1", "server-2"], "strategy": {"type": "random"}}
    ]
    after = apply_balancers(tpl, SPECS)
    balancers = after["routing"]["balancers"]
    assert balancers[0]["tag"] == "user-balancer"
    ours = [b for b in balancers if b["tag"] in TAGS]
    assert len(ours) == 3
    for bal in ours:
        assert len(bal["selector"]) == 1  # §12
        assert bal["fallbackTag"] == "server-4"

    # повторное применение не меняет результат (идемпотентность)
    assert canonical(apply_balancers(after, SPECS)) == canonical(after)


def test_observatory_created_and_extended() -> None:
    after, kind = apply_observatory(template(), SPECS, ObservatoryConfigModel())
    assert kind == "observatory"
    assert after["observatory"]["subjectSelector"] == ["server-1", "server-2", "server-3", "server-4"]
    assert after["observatory"]["probeURL"]

    user_obs = template()
    user_obs["observatory"] = {
        "subjectSelector": ["user-node"],
        "probeURL": "https://cp.cloudflare.com/generate_204",
        "probeInterval": "15s",
    }
    after2, _ = apply_observatory(user_obs, SPECS, ObservatoryConfigModel())
    assert after2["observatory"]["subjectSelector"] == [
        "server-1",
        "server-2",
        "server-3",
        "server-4",
        "user-node",
    ]
    assert after2["observatory"]["probeURL"] == "https://cp.cloudflare.com/generate_204"
    assert after2["observatory"]["probeInterval"] == "15s"
    assert canonical(apply_observatory(after2, SPECS, ObservatoryConfigModel())[0]) == canonical(after2)


def test_observatory_for_round_robin_with_fallback_is_burst() -> None:
    specs = [
        BalancerSpec(tag="client-balancer-1", primary="server-1", fallback="server-4", strategy="roundRobin")
    ]
    assert observatory_requirement(specs, "auto") == "burstObservatory"
    after, kind = apply_observatory(template(), specs, ObservatoryConfigModel())
    assert kind == "burstObservatory"
    assert after["burstObservatory"]["subjectSelector"] == ["server-1", "server-4"]
    assert after["burstObservatory"]["pingConfig"]["destination"]


def test_observatory_is_never_deleted() -> None:
    tpl = template()
    tpl["observatory"] = {"subjectSelector": ["user-node"], "probeURL": "https://x/204"}
    after, _ = apply_observatory(tpl, SPECS, ObservatoryConfigModel(type="burst"))
    assert "observatory" in after

    tpl2 = template()
    after2, _ = apply_observatory(tpl2, SPECS, ObservatoryConfigModel(type="burst"))
    assert "observatory" not in after2


def test_validate_requires_single_selector() -> None:
    tpl = apply_balancers(template(), SPECS)
    tpl["routing"]["balancers"][-1]["selector"] = ["server-1", "server-2", "server-3"]
    problems = validate_candidate(tpl, GROUPS, SPECS)
    assert any("ровно один outbound" in p for p in problems)


def test_validate_detects_missing_outbound() -> None:
    tpl = template()
    tpl["outbounds"] = [{"tag": "server-1"}, {"tag": "server-4"}]
    candidate = apply_managed_block(tpl, managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    candidate = apply_balancers(candidate, SPECS)
    problems = validate_candidate(candidate, GROUPS, SPECS)
    assert sum("отсутствует и в шаблоне" in p for p in problems) == 2  # server-2 и server-3


def test_validate_detects_duplicate_user() -> None:
    tpl = apply_managed_block(template(), managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    tpl = apply_balancers(tpl, SPECS)
    tpl["routing"]["rules"][-1]["user"] = ["a@example"]
    problems = validate_candidate(tpl, GROUPS, SPECS)
    assert any("одновременно" in p for p in problems)


def test_validate_detects_empty_user_list() -> None:
    tpl = apply_managed_block(template(), managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    tpl = apply_balancers(tpl, SPECS)
    tpl["routing"]["rules"][-1]["user"] = []
    problems = validate_candidate(tpl, GROUPS, SPECS)
    assert any("непустого списка user" in p for p in problems)


def test_validate_clean_candidate_has_no_problems() -> None:
    candidate = apply_observatory(
        apply_balancers(
            apply_managed_block(template(), managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS), SPECS
        ),
        SPECS,
        ObservatoryConfigModel(),
    )[0]
    assert validate_candidate(candidate, GROUPS, SPECS) == []


def test_is_managed_by_comment_even_without_balancer_tag() -> None:
    rule = {"type": "field", "user": ["a@example"], "comment": f"{MANAGED_COMMENT_PREFIX}client-balancer-1"}
    assert is_managed_rule(rule, TAGS)


def test_template_changed_is_canonical() -> None:
    a = template()
    b = copy.deepcopy(a)
    assert not template_changed(a, b)
    b["routing"]["rules"][0]["outboundTag"] = "direct"
    assert template_changed(a, b)


def test_describe_managed_block() -> None:
    after = apply_managed_block(template(), managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    described = describe_managed_block(after, TAGS)
    assert [(d["index"], d["users"]) for d in described] == [(3, 2), (4, 1)]


def test_idempotent_second_apply() -> None:
    once = apply_managed_block(template(), managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    twice = apply_managed_block(once, managed_block(GROUPS, SPECS, ROUTING), ROUTING, TAGS)
    assert canonical(once) == canonical(twice)
