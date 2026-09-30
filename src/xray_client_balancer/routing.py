"""Управление только собственным блоком конфига Xray (§7–§14, §33, §47, §49).

Что делает модуль:
  * помечает «свои» routing-правила (balancerTag из конфига сервиса + comment
    `xcb-managed:<tag>`; панель 3x-ui вырезает comment/enabled при генерации);
  * перестраивает только свои правила, сохраняя правила пользователя,
    их порядок и все прочие секции конфига;
  * поддерживает свои балансировщики (у каждого selector ровно один outbound —
    это то, что не даёт Xray выбирать outbound на каждое соединение, §12);
  * поддерживает healthcheck (observatory/burstObservatory), без которого
    fallbackTag не работает: Xray требует observatory, если у стратегии задан
    fallbackTag;
  * считает подписи «чужой» части конфига, чтобы обнаружить потерянное
    обновление (§9) и отличить его от собственных правок.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .config import ObservatoryConfigModel, RoutingConfig
from .models import BalancerSpec, sort_emails

logger = logging.getLogger(__name__)

MANAGED_COMMENT_PREFIX = "xcb-managed:"
RULE_TAG_PREFIX = "xcb-rule:"
OBSERVATORY_TYPES_REQUIRING_BURST = {"leastload"}
OBSERVATORY_STRATEGIES_WITH_FALLBACK = {"random", "roundrobin"}


class ConfigValidationError(RuntimeError):
    """Кандидат конфига невалиден — применять его нельзя (§10, §33)."""


def canonical(value: Any) -> str:
    """Канонический JSON (ключи отсортированы) — сравнение без учёта форматирования."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- managed rules


def is_managed_rule(rule: Mapping[str, Any], balancer_tags: Iterable[str]) -> bool:
    """Своё ли это правило.

    Основной признак — balancerTag принадлежит сервису (структурный, не зависит
    от того, вырезала ли панель служебные ключи). Дополнительно принимаем
    comment-маркер: правило, у которого пользователь снял balancerTag, всё равно
    остаётся нашим и должно быть починено (§25).
    """
    tags = set(balancer_tags)
    if str(rule.get("balancerTag") or "") in tags:
        return True
    comment = str(rule.get("comment") or "")
    return comment.startswith(MANAGED_COMMENT_PREFIX)


def managed_rule_tag(balancer_tag: str) -> str:
    return f"{RULE_TAG_PREFIX}{balancer_tag}"


def build_managed_rule(balancer_tag: str, emails: Sequence[str], routing: RoutingConfig) -> dict[str, Any]:
    """Правило вида {type, user[], balancerTag} + служебные ключи панели."""
    sorted_emails = sort_emails(emails)
    if not sorted_emails:
        raise ConfigValidationError(f"{balancer_tag}: пустой список пользователей")
    rule: dict[str, Any] = {
        "type": "field",
        "user": sorted_emails,
        "balancerTag": balancer_tag,
    }
    if routing.write_rule_tag:
        rule["ruleTag"] = managed_rule_tag(balancer_tag)
    rule["comment"] = f"{MANAGED_COMMENT_PREFIX}{balancer_tag}"
    rule["enabled"] = True
    return rule


def managed_block(
    groups: Mapping[str, Sequence[str]],
    specs: Sequence[BalancerSpec],
    routing: RoutingConfig,
) -> list[dict[str, Any]]:
    """Собрать наш блок правил. Группы без клиентов правил не получают.

    Пустой `user: []` у Xray не означает «никого» — правило без matcher'ов
    совпало бы со всем трафиком, поэтому пустые правила запрещены.
    """
    block: list[dict[str, Any]] = []
    for spec in specs:
        emails = list(groups.get(spec.tag, []))
        if not emails:
            continue
        block.append(build_managed_rule(spec.tag, emails, routing))
    return block


def find_position_indices(rules: Sequence[Mapping[str, Any]], balancer_tags: Iterable[str]) -> list[int]:
    return [i for i, rule in enumerate(rules) if isinstance(rule, dict) and is_managed_rule(rule, balancer_tags)]


def _marker_matches(rule: Mapping[str, Any], marker: str) -> bool:
    for key in ("ruleTag", "comment", "balancerTag", "outboundTag", "inboundTag", "id"):
        value = rule.get(key)
        if isinstance(value, str) and marker in value:
            return True
        if isinstance(value, (list, tuple)) and any(marker in str(v) for v in value):
            return True
    return False


def target_insert_index(
    foreign_rules: Sequence[Mapping[str, Any]],
    routing: RoutingConfig,
    existing_index: int | None,
) -> int:
    """Куда поставить блок: сохраняем существующую позицию, иначе считаем по конфигу."""
    if existing_index is not None:
        return max(0, min(existing_index, len(foreign_rules)))
    if routing.managed_position == "top":
        return 0
    if routing.managed_position == "after_rule" and routing.insert_after_rule:
        marker = routing.insert_after_rule
        for i, rule in enumerate(foreign_rules):
            if _marker_matches(rule, marker):
                return i + 1
        logger.warning(
            "Не найден маркер routing.insert_after_rule=%r — блок правил поставлен в конец", marker
        )
    return len(foreign_rules)


def apply_managed_block(
    template: Mapping[str, Any],
    block: Sequence[Mapping[str, Any]],
    routing: RoutingConfig,
    balancer_tags: Iterable[str],
) -> dict[str, Any]:
    """Вернуть копию шаблона с нашим блоком правил на нужной позиции."""
    new_template = json.loads(json.dumps(template))
    section = new_template.setdefault("routing", {})
    if not isinstance(section, dict):
        raise ConfigValidationError("секция routing в шаблоне не объект")
    rules = section.setdefault("rules", [])
    if not isinstance(rules, list):
        raise ConfigValidationError("routing.rules не массив")

    indices = find_position_indices(rules, balancer_tags)
    if indices:
        existing_index = indices[0]
    else:
        existing_index = None
    foreign = [r for i, r in enumerate(rules) if i not in set(indices)]
    index = target_insert_index(foreign, routing, existing_index)
    section["rules"] = foreign[:index] + [dict(r) for r in block] + foreign[index:]
    return new_template


# --------------------------------------------------------------------- balancers


def observatory_requirement(specs: Sequence[BalancerSpec], mode: str) -> str:
    """Какой healthcheck нужен нашим балансировщикам: 'observatory' или 'burstObservatory'."""
    if mode == "burst":
        return "burstObservatory"
    if mode == "observatory":
        return "observatory"
    for spec in specs:
        strategy = spec.strategy.lower()
        if strategy in OBSERVATORY_TYPES_REQUIRING_BURST:
            return "burstObservatory"
        if strategy in OBSERVATORY_STRATEGIES_WITH_FALLBACK and spec.fallback:
            return "burstObservatory"
    return "observatory"


def build_balancer_objects(specs: Sequence[BalancerSpec]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for spec in specs:
        strategy: dict[str, Any] = {"type": spec.strategy}
        if spec.strategy_settings:
            # Xray сам приводит тип к нижнему регистру и допускает пустые settings,
            # но для leastLoad настройки важны (baselines/expected/tolerance/maxRTT)
            strategy["settings"] = dict(spec.strategy_settings)
        objects.append(
            {
                "tag": spec.tag,
                "selector": spec.selector,
                "strategy": strategy,
                "fallbackTag": spec.fallback,
            }
        )
    return objects


def apply_balancers(
    template: Mapping[str, Any], specs: Sequence[BalancerSpec]
) -> dict[str, Any]:
    """Заменить только свои балансировщики, чужие (например панельные) сохранить."""
    new_template = json.loads(json.dumps(template))
    section = new_template.setdefault("routing", {})
    if not isinstance(section, dict):
        raise ConfigValidationError("секция routing в шаблоне не объект")
    ours = {spec.tag for spec in specs}
    existing = section.get("balancers") or []
    if not isinstance(existing, list):
        raise ConfigValidationError("routing.balancers не массив")
    foreign = [b for b in existing if not (isinstance(b, dict) and b.get("tag") in ours)]
    section["balancers"] = foreign + build_balancer_objects(specs)
    return new_template


def apply_observatory(
    template: Mapping[str, Any],
    specs: Sequence[BalancerSpec],
    obs: ObservatoryConfigModel,
) -> tuple[dict[str, Any], str]:
    """Дописать свои outbound-теги в subjectSelector нужной секции healthcheck.

    Чужие теги и настройки сохраняются; секция никогда не удаляется.
    """
    new_template = json.loads(json.dumps(template))
    required = observatory_requirement(specs, obs.type)
    our_tags = sorted({spec.primary for spec in specs} | {spec.fallback for spec in specs})

    if required == "burstObservatory":
        section = new_template.get("burstObservatory")
        if not isinstance(section, dict):
            section = {
                "subjectSelector": [],
                "pingConfig": {
                    "destination": obs.probe_url,
                    "interval": obs.burst_interval,
                    "timeout": obs.burst_timeout,
                    "sampling": obs.burst_sampling,
                    "httpMethod": obs.burst_http_method,
                },
            }
        current = section.get("subjectSelector") or []
        if not isinstance(current, list):
            current = []
        section["subjectSelector"] = sorted({str(t) for t in current} | set(our_tags))
        if new_template.get("observatory") is not None:
            logger.warning(
                "В шаблоне есть observatory, а стратегии сервиса требуют burstObservatory: "
                "наши балансировщики могут получить результаты обычного observatory"
            )
        new_template["burstObservatory"] = section
        return new_template, required

    section = new_template.get("observatory")
    if not isinstance(section, dict):
        section = {
            "subjectSelector": [],
            "probeURL": obs.probe_url,
            "probeInterval": obs.probe_interval,
            "enableConcurrency": True,
        }
    current = section.get("subjectSelector") or []
    if not isinstance(current, list):
        current = []
    section["subjectSelector"] = sorted({str(t) for t in current} | set(our_tags))
    if not section.get("probeURL"):
        section["probeURL"] = obs.probe_url
    if not section.get("probeInterval"):
        section["probeInterval"] = obs.probe_interval
    new_template["observatory"] = section
    return new_template, required


# --------------------------------------------------------------------- validation


def collect_outbound_tags(template: Mapping[str, Any]) -> set[str]:
    outbounds = template.get("outbounds") or []
    tags = set()
    if isinstance(outbounds, list):
        for ob in outbounds:
            if isinstance(ob, dict) and ob.get("tag"):
                tags.add(str(ob["tag"]))
    return tags


def validate_candidate(
    template: Mapping[str, Any],
    groups: Mapping[str, Sequence[str]],
    specs: Sequence[BalancerSpec],
    *,
    require_outbounds: bool = True,
    known_outbound_tags: Iterable[str] | None = None,
) -> list[str]:
    """Проверить кандидат до записи. Возвращает список проблем (пусто = всё хорошо).

    known_outbound_tags — дополнительные outbound-теги, которых нет в шаблоне, но
    которые есть в работающем конфиге (в 3.8.x outbound'ы могут подставляться из
    подписок/таблиц панели, как на узле hub.main: теги sub*-tls-* живут вне шаблона).
    """
    problems: list[str] = []
    section = template.get("routing")
    if not isinstance(section, dict):
        return ["routing отсутствует или не объект"]
    rules = section.get("rules")
    if not isinstance(rules, list):
        return ["routing.rules не массив"]

    specs_by_tag = {spec.tag: spec for spec in specs}
    seen_emails: dict[str, str] = {}
    for rule in rules:
        if not isinstance(rule, dict) or not is_managed_rule(rule, specs_by_tag):
            continue
        tag = str(rule.get("balancerTag") or "")
        if tag not in specs_by_tag:
            if str(rule.get("comment") or "").startswith(MANAGED_COMMENT_PREFIX):
                problems.append(f"managed-правило ссылается на неизвестный balancerTag {tag!r}")
            continue
        emails = rule.get("user")
        if not isinstance(emails, list) or not emails:
            problems.append(f"{tag}: правило без непустого списка user (в Xray это совпало бы со всем трафиком)")
            continue
        if list(emails) != sort_emails(emails):
            problems.append(f"{tag}: список пользователей не отсортирован (§28)")
        for email in emails:
            prev = seen_emails.get(str(email))
            if prev:
                problems.append(f"клиент {email} назначен одновременно в {prev} и {tag}")
            seen_emails[str(email)] = tag
        expected = sort_emails(groups.get(tag, []))
        if expected and list(emails) != expected:
            problems.append(f"{tag}: список пользователей в правиле не совпадает с планом")

    balancers = section.get("balancers") or []
    if not isinstance(balancers, list):
        problems.append("routing.balancers не массив")
    else:
        by_tag = {str(b.get("tag")): b for b in balancers if isinstance(b, dict)}
        for spec in specs:
            obj = by_tag.get(spec.tag)
            if obj is None:
                problems.append(f"{spec.tag}: балансировщик отсутствует в routing.balancers")
                continue
            selector = obj.get("selector")
            if not isinstance(selector, list) or len(selector) != 1:
                problems.append(
                    f"{spec.tag}: selector должен содержать ровно один outbound (§12), сейчас {selector!r}"
                )
            elif selector[0] != spec.primary:
                problems.append(f"{spec.tag}: selector={selector[0]!r}, ожидался primary={spec.primary!r}")
            if str(obj.get("fallbackTag") or "") != spec.fallback:
                problems.append(f"{spec.tag}: fallbackTag не равен {spec.fallback!r}")

    if require_outbounds:
        known = collect_outbound_tags(template) | set(known_outbound_tags or ())
        if not known:
            problems.append("нет известных outbounds — нечего выбирать балансировщикам")
        else:
            for spec in specs:
                for tag in (spec.primary, spec.fallback):
                    if tag not in known:
                        problems.append(
                            f"outbound {tag!r} (нужен {spec.tag}) отсутствует и в шаблоне, и в "
                            f"работающем конфиге: есть {sorted(known)}"
                        )

    return problems


# --------------------------------------------------------------------- foreign signature


OBSERVATORY_SECTIONS = ("observatory", "burstObservatory")
OBSERVATORY_STANDARD_KEYS = {
    "observatory": {"subjectSelector", "probeURL", "probeInterval", "enableConcurrency"},
    "burstObservatory": {"subjectSelector", "pingConfig"},
}


def _observatory_view(section: Any, name: str, our_tags: set[str]) -> Any:
    """Проекция healthcheck-секции без нашего вклада.

    Сервис только добавляет свои outbound-теги в subjectSelector и никогда ничего
    оттуда не удаляет, поэтому «чужой» считается секция без наших тегов; секция,
    состоящая только из наших тегов и стандартных ключей, считается целиком нашей.
    """
    if not isinstance(section, dict):
        return None
    view = {k: v for k, v in section.items() if k != "subjectSelector"}
    others = [t for t in (section.get("subjectSelector") or []) if str(t) not in our_tags]
    if not others and set(section.keys()) <= OBSERVATORY_STANDARD_KEYS.get(name, set()):
        return None
    view["subjectSelector"] = others
    return view


def routing_shell(section: Mapping[str, Any]) -> dict[str, Any]:
    """Часть routing, которой сервис не управляет (domainStrategy и прочее)."""
    return {k: v for k, v in section.items() if k not in ("rules", "balancers")}


def foreign_projection(
    template: Mapping[str, Any],
    balancer_tags: Iterable[str],
    observatory_tags: Iterable[str] = (),
) -> dict[str, Any]:
    """Проекция конфига без наших правил/балансировщиков/нашего healthcheck.

    `observatory_tags` — outbound-теги, которые сервис добавляет в subjectSelector;
    только они вычитаются из «чужой» части healthcheck-секции.
    """
    tags = set(balancer_tags)
    ours_observatory = set(observatory_tags)
    rules = []
    balancers = []
    section = template.get("routing")
    if isinstance(section, dict):
        for rule in section.get("rules") or []:
            if isinstance(rule, dict) and is_managed_rule(rule, tags):
                continue
            rules.append(rule)
        for bal in section.get("balancers") or []:
            if isinstance(bal, dict) and str(bal.get("tag") or "") in tags:
                continue
            balancers.append(bal)
        shell = routing_shell(section)
    else:
        shell = {}
    other = {k: v for k, v in template.items() if k not in ("routing", *OBSERVATORY_SECTIONS)}
    observatory = {
        name: _observatory_view(template.get(name), name, ours_observatory)
        for name in OBSERVATORY_SECTIONS
    }
    return {
        "rules": rules,
        "balancers": balancers,
        "routing_shell": shell,
        "sections": other,
        "observatory": observatory,
    }


def foreign_signature(
    template: Mapping[str, Any],
    balancer_tags: Iterable[str],
    observatory_tags: Iterable[str] = (),
) -> str:
    """Подпись чужой части как МУЛЬТИМНОЖЕСТВА: панель может менять порядок правил
    (например поднимает своё api-правило наверх), и это не потеря обновления."""
    projection = foreign_projection(template, balancer_tags, observatory_tags)
    return digest(
        {
            "rules": sorted(canonical(r) for r in projection["rules"]),
            "balancers": sorted(canonical(b) for b in projection["balancers"]),
            "routing_shell": projection["routing_shell"],
            "sections": projection["sections"],
            "observatory": projection["observatory"],
        }
    )


@dataclass
class ForeignDiff:
    added_rules: list[Any]
    removed_rules: list[Any]
    sections_changed: list[str]

    @property
    def empty(self) -> bool:
        return not (self.added_rules or self.removed_rules or self.sections_changed)


def foreign_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    balancer_tags: Iterable[str],
    observatory_tags: Iterable[str] = (),
) -> ForeignDiff:
    """Что изменилось в чужой части между двумя снимками конфига."""
    b = foreign_projection(before, balancer_tags, observatory_tags)
    a = foreign_projection(after, balancer_tags, observatory_tags)
    b_rules = sorted(canonical(r) for r in b["rules"])
    a_rules = sorted(canonical(r) for r in a["rules"])
    added = [r for r in a_rules if r not in b_rules]
    removed = [r for r in b_rules if r not in a_rules]
    changed_sections = [
        key
        for key in ("sections", "routing_shell", "observatory")
        if canonical(b[key]) != canonical(a[key])
    ]
    changed_balancers = canonical(b["balancers"]) != canonical(a["balancers"])
    if changed_balancers:
        changed_sections.append("balancers")
    return ForeignDiff(added_rules=added, removed_rules=removed, sections_changed=changed_sections)


def template_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return canonical(before) != canonical(after)


def describe_managed_block(template: Mapping[str, Any], balancer_tags: Iterable[str]) -> list[dict[str, Any]]:
    """Компактное описание наших правил — для логов, статуса и --dry-run."""
    section = template.get("routing") if isinstance(template.get("routing"), dict) else {}
    out: list[dict[str, Any]] = []
    for index, rule in enumerate(section.get("rules") or []):
        if isinstance(rule, dict) and is_managed_rule(rule, balancer_tags):
            out.append(
                {
                    "index": index,
                    "balancerTag": rule.get("balancerTag"),
                    "users": len(rule.get("user") or []),
                }
            )
    return out
