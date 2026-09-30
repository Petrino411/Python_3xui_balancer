"""Allocator: чистая логика распределения клиентов по балансировщикам (§39, §40).

Модуль не знает ни про API, ни про SQLite — только входные данные и результат.
Никакого random: при равной загрузке порядок определяется порядком тегов из конфига.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping, Sequence

from .models import Assignment, BalancerSpec, PanelClient, SyncPlan, group_assignments, sort_emails


def balancer_order_index(specs: Sequence[BalancerSpec]) -> dict[str, int]:
    """Детерминированный tie-break: индекс тега в конфиге."""
    return {spec.tag: i for i, spec in enumerate(specs)}


def pick_balancer(
    counts: Mapping[str, int],
    specs: Sequence[BalancerSpec],
    order_index: Mapping[str, int] | None = None,
) -> str:
    """Группа с минимальным количеством клиентов; при равенстве — первая по порядку конфига."""
    if not specs:
        raise ValueError("нет ни одного балансировщика")
    index = order_index or balancer_order_index(specs)
    return min(
        (spec.tag for spec in specs),
        key=lambda tag: (counts.get(tag, 0), index.get(tag, 0), tag),
    )


def assign_new_clients(
    existing_assignments: Mapping[int, Assignment],
    current_clients: Iterable[PanelClient],
    balancers: Sequence[BalancerSpec],
) -> dict[int, str]:
    """Назначить только новых клиентов (§4).

    Уже существующие назначения не меняются: расхождение в 1 клиента — норма (§3).
    Новые клиенты обрабатываются в стабильном порядке (по client_id), поэтому
    результат детерминирован и на первом запуске, и после восстановления БД.
    """
    counts = {spec.tag: 0 for spec in balancers}
    for assignment in existing_assignments.values():
        if assignment.balancer_tag in counts:
            counts[assignment.balancer_tag] += 1
    index = balancer_order_index(balancers)

    result: dict[int, str] = {}
    new_clients = sorted(
        (c for c in current_clients if c.client_id not in existing_assignments),
        key=lambda c: (c.client_id, c.email.lower()),
    )
    for client in new_clients:
        tag = pick_balancer(counts, balancers, index)
        result[client.client_id] = tag
        counts[tag] += 1
    return result


def rebalance_assignments(
    current_clients: Iterable[PanelClient],
    balancers: Sequence[BalancerSpec],
) -> dict[int, str]:
    """Ручной rebalance (§21): разложить всех клиентов максимально ровно, детерминированно."""
    specs = list(balancers)
    index = balancer_order_index(specs)
    counts = {spec.tag: 0 for spec in specs}
    result: dict[int, str] = {}
    for client in sorted(current_clients, key=lambda c: (c.client_id, c.email.lower())):
        tag = pick_balancer(counts, specs, index)
        result[client.client_id] = tag
        counts[tag] += 1
    return result


def build_plan(
    existing_assignments: Mapping[int, Assignment],
    clients: Sequence[PanelClient],
    balancers: Sequence[BalancerSpec],
    *,
    include_disabled: bool = True,
    is_excluded: Callable[[str], bool] = lambda email: False,
) -> SyncPlan:
    """Собрать план синхронизации: назначения, удаления, смена email, группы для routing."""
    specs = list(balancers)
    order = [spec.tag for spec in specs]

    excluded = [c.email for c in clients if is_excluded(c.email)]
    # исключённые (§46) и, при include_disabled=false, неактивные клиенты не участвуют
    # ни в распределении, ни в правилах; их строки в БД при этом не удаляются.
    eligible = [
        c for c in clients if not is_excluded(c.email) and (include_disabled or c.active)
    ]
    eligible_ids = {c.client_id for c in eligible}
    known = {c.client_id: c for c in eligible}

    plan = SyncPlan(order=order, excluded_emails=excluded)

    # 1. удаления: назначение снимается только если клиент реально исчез из панели (§5, §27)
    existing_ids = {c.client_id for c in clients}
    for client_id in sorted(existing_assignments):
        if client_id not in existing_ids:
            plan.removed_client_ids.append(client_id)

    # 2. новые назначения — только по клиентам, которые реально управляются сервисом
    managed_existing = {
        cid: a for cid, a in existing_assignments.items() if cid in eligible_ids
    }
    plan.new_assignments = assign_new_clients(managed_existing, eligible, specs)

    # 3. смена email у существующего клиента (внутренний ID тот же) (§6)
    for client_id, assignment in managed_existing.items():
        client = known.get(client_id)
        if client is not None and client.email != assignment.email:
            plan.email_updates[client_id] = client.email

    # 4. группы для managed routing rules: назначения из БД + только что принятые решения
    effective: list[Assignment] = []
    for client_id, assignment in managed_existing.items():
        email = plan.email_updates.get(client_id, assignment.email)
        effective.append(
            Assignment(
                client_id=client_id,
                email=email,
                balancer_tag=assignment.balancer_tag,
                created_at=assignment.created_at,
                updated_at=assignment.updated_at,
            )
        )
    for client_id, tag in plan.new_assignments.items():
        client = known.get(client_id)
        if client is None:
            continue
        effective.append(
            Assignment(
                client_id=client_id, email=client.email, balancer_tag=tag, created_at=0, updated_at=0
            )
        )

    plan.groups = group_assignments(effective, order)
    return plan


def distributed_evenly(counts: Mapping[str, int]) -> bool:
    """Проверка §2: разница между группами не больше одного клиента."""
    if not counts:
        return True
    values = list(counts.values())
    return max(values) - min(values) <= 1


def rebalance_diff(
    existing_assignments: Mapping[int, Assignment], target: Mapping[int, str]
) -> dict[int, str]:
    """Какие назначения изменятся при rebalance (для предупреждения перед --yes)."""
    changed: dict[int, str] = {}
    for client_id, tag in target.items():
        current = existing_assignments.get(client_id)
        if current is None or current.balancer_tag != tag:
            changed[client_id] = tag
    return changed


def sorted_unique_emails(emails: Iterable[str]) -> list[str]:
    return sort_emails(set(emails))
