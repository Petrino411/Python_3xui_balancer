"""Точечное переназначение клиентов между балансировщиками (CLI `move`).

`rebalance` перекладывает всех клиентов заново (§21), а `move` меняет назначение
только указанных клиентов и не трогает остальных: sticky-привязка §3 остаётся в силе.
Меняются ровно две строки в состоянии, а из этого получается правка одного-двух
правил routing — такие правки панель применяет hot-apply, без перезапуска ядра.

Модуль не общается с панелью напрямую: он принимает уже собранный
`BalancerService`, поэтому ту же логику используют и CLI, и tools/move_client.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .api import PanelApi, PanelError
from .config import AppConfig
from .models import Assignment, PanelClient
from .service import BalancerService, SyncReport, distribution_of

# Код выхода для «операцию поняли, но выполнить нельзя» (нет клиента, нет тега,
# клиент исключён конфигом) — отдельно от «сервис сломался» (1).
EXIT_REFUSED = 2


class MoveError(RuntimeError):
    """Переназначение выполнить нельзя. Сообщение печатается пользователю как есть."""

    def __init__(self, message: str, *, hint: str = "", exit_code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.hint = hint
        self.exit_code = exit_code


# --------------------------------------------------------------------- выбор клиента


def resolve_client(
    clients: Sequence[PanelClient],
    query: str = "",
    *,
    client_id: int | None = None,
    allow_substring: bool = True,
) -> PanelClient:
    """Найти клиента по id или по email (точное совпадение, затем однозначная подстрока).

    Неоднозначность — это отказ, а не догадка: неверный выбор здесь стоил бы свапа
    не того клиента.
    """
    if client_id is not None:
        matches = [c for c in clients if c.client_id == client_id]
        if not matches:
            raise MoveError(
                f"клиента с id={client_id} нет в панели",
                hint=f"всего клиентов: {len(clients)}; список: xcb clients",
            )
        return matches[0]

    text = (query or "").strip()
    if not text:
        raise MoveError("не указан клиент: укажите email или --id N")

    exact = [c for c in clients if c.email == text]
    if not exact:
        exact = [c for c in clients if c.email.lower() == text.lower()]
    if len(exact) > 1:
        raise MoveError(
            f"email '{text}' встречается {len(exact)} раз (id: {[c.client_id for c in exact]})",
            hint="используйте --id N",
        )
    if exact:
        return exact[0]

    if not allow_substring:
        raise MoveError(f"клиент '{text}' не найден")

    partial = [c for c in clients if text.lower() in c.email.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise MoveError(
            f"клиент '{text}' не найден",
            hint="список клиентов: xcb clients"
            + (f"; похожие: {', '.join(c.email for c in clients[:5])}" if clients else ""),
        )
    raise MoveError(
        f"подстрока '{text}' подходит к {len(partial)} клиентам",
        hint="уточните: " + ", ".join(f"{c.email} (id={c.client_id})" for c in partial[:6]),
    )


def find_balancer_tag(tags: Sequence[str], value: str) -> str:
    """Тег по имени: полный, либо суффикс группы (1/2/3), либо однозначная подстрока."""
    text = (value or "").strip()
    if text in tags:
        return text
    lowered = text.lower()
    for tag in tags:
        if tag.lower() == lowered:
            return tag
    for tag in tags:
        if tag.lower().endswith(f"-{lowered}") or tag.lower().endswith(lowered):
            return tag
    partial = [tag for tag in tags if lowered and lowered in tag.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise MoveError(
            f"'{text}' подходит к нескольким балансировщикам: {partial}",
            hint=f"укажите полный тег: {', '.join(tags)}",
        )
    raise MoveError(
        f"балансировщика '{text}' нет в конфиге сервиса",
        hint=f"доступные теги: {', '.join(tags)}; посмотреть раскладку: xcb balancers",
    )


# --------------------------------------------------------------------- план


@dataclass(frozen=True)
class MovePlan:
    """Что именно изменится для одного клиента."""

    client: PanelClient
    current_tag: str | None
    target_tag: str
    counts_before: dict[str, int]
    counts_after: dict[str, int]
    warnings: tuple[str, ...] = ()

    @property
    def is_noop(self) -> bool:
        return self.current_tag == self.target_tag

    @property
    def is_new(self) -> bool:
        """У клиента ещё нет sticky-назначения (сервис назначил бы его сам)."""
        return self.current_tag is None

    def describe(self) -> str:
        before = self.current_tag or "— (назначения нет)"
        return f"{self.client.email} (id={self.client.client_id}): {before} -> {self.target_tag}"


def check_eligibility(config: AppConfig, client: PanelClient) -> list[str]:
    """Проверить, будет ли клиент вообще участвовать в правилах. Возвращает предупреждения."""
    if config.is_excluded(client.email):
        raise MoveError(
            f"клиент '{client.email}' исключён конфигом (exclude_clients/exclude_regex) — "
            "назначение такого клиента сервис не пишет в routing",
            hint="уберите его из exclude_clients в /etc/xray-client-balancer/config.yaml",
        )
    warnings: list[str] = []
    if not client.active:
        if config.include_disabled:
            warnings.append(
                f"{client.email}: клиент неактивен (disabled/expired/исчерпан трафик), "
                "но include_disabled=true — правило останется в конфиге"
            )
        else:
            warnings.append(
                f"{client.email}: клиент неактивен, а include_disabled=false — sticky-назначение "
                "сохранится, но в routing он не попадёт, пока не станет активным"
            )
    return warnings


def plan_moves(
    assignments: Mapping[int, Assignment],
    clients: Sequence[PanelClient],
    targets: Mapping[int, str],
    tags: Sequence[str],
) -> list[MovePlan]:
    """Собрать план: раскладка до/после считается последовательно по всем клиентам."""
    counts = distribution_of(assignments, tags)
    plans: list[MovePlan] = []
    for client in clients:
        target = targets[client.client_id]
        current = assignments.get(client.client_id)
        before = dict(counts)
        current_tag = current.balancer_tag if current and current.balancer_tag in counts else None
        after = dict(counts)
        if current_tag:
            after[current_tag] -= 1
        after[target] = after.get(target, 0) + 1
        plans.append(
            MovePlan(
                client=client,
                current_tag=current_tag,
                target_tag=target,
                counts_before=before,
                counts_after=after,
            )
        )
        if not (current_tag == target):
            counts = after
    return plans


def pick_target_counted(counts: Mapping[str, int], current_tag: str | None, tags: Sequence[str]) -> str:
    """Самая свободная группа, кроме текущей (при равенстве — первая по конфигу)."""
    candidates = [tag for tag in tags if tag != current_tag] or list(tags)
    index = {tag: i for i, tag in enumerate(tags)}
    return min(candidates, key=lambda tag: (counts.get(tag, 0), index.get(tag, 0)))


def pick_target(assignments: Mapping[int, Assignment], client: PanelClient, tags: Sequence[str]) -> str:
    """Цель для --auto: группа с минимумом клиентов, кроме текущей."""
    current = assignments.get(client.client_id)
    return pick_target_counted(
        distribution_of(assignments, tags), current.balancer_tag if current else None, tags
    )


# --------------------------------------------------------------------- применение


@dataclass
class MoveOutcome:
    """Результат применения: что изменилось, что записалось, что подтвердило ядро."""

    moved: list[tuple[str, str, str]] = field(default_factory=list)  # (email, было, стало)
    db_changed: bool = False
    config_written: bool = False
    routing_planned_change: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    route_checks: list[tuple[str, bool | None, str]] = field(default_factory=list)
    live_status: dict[str, object] = field(default_factory=dict)
    uptime_before: int | None = None
    uptime_after: int | None = None
    sync_report: SyncReport | None = None

    @property
    def core_restarted(self) -> bool | None:
        """Перезапустилось ли ядро: правка только routing должна проходить hot-apply."""
        if self.uptime_before is None or self.uptime_after is None:
            return None
        return self.uptime_after < self.uptime_before

    def render(self) -> str:
        lines: list[str] = []
        for email, was, now in self.moved:
            lines.append(f"{email}: {was} -> {now}")
        lines.append("")
        lines.append(f"Локальное состояние обновлено: {'да' if self.db_changed else 'нет'}")
        lines.append(f"Конфиг записан в панель: {'да' if self.config_written else 'нет'}")
        if self.core_restarted is not None:
            lines.append(
                f"Ядро: uptime {self.uptime_before} -> {self.uptime_after} "
                f"(перезапуск: {'ДА' if self.core_restarted else 'нет — hot-apply'})"
            )
        if self.routing_planned_change and not self.config_written and not self.errors:
            lines.append("Конфиг уже соответствовал плану — запись не потребовалась")
        for email, ok, detail in self.route_checks:
            if ok is True:
                lines.append(f"routeTest {email}: маршрут подтверждён ({detail})")
            elif ok is None:
                lines.append(f"routeTest {email}: НЕ ПРОВЕРЕНО — {detail}")
            else:
                lines.append(f"routeTest {email}: ОШИБКА — {detail}")
        for tag, info in self.live_status.items():
            if isinstance(info, dict):
                lines.append(
                    f"балансировщик {tag}: running={info.get('running')} "
                    f"selected={info.get('selected')}"
                )
        for warning in self.warnings:
            lines.append(f"WARNING: {warning}")
        for error in self.errors:
            lines.append(f"ERROR: {error}")
        return "\n".join(lines)


def _uptime(api: PanelApi) -> int | None:
    try:
        value = api.xray_state().get("uptime")
    except PanelError:
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):  # pragma: no cover - панель вернула нечисло
        return None


def apply_moves(
    service: BalancerService,
    plans: Iterable[MovePlan],
    *,
    verify: bool = True,
) -> MoveOutcome:
    """Записать назначения и сделать ОДИН цикл синхронизации на все правки.

    Один цикл, а не по циклу на клиента: панель перезапускает ядро на каждую
    принятую запись конфига, поэтому пакетная правка дешевле.
    """
    outcome = MoveOutcome()
    effective = [plan for plan in plans if not plan.is_noop]
    if not effective:
        outcome.warnings.append("все клиенты уже в целевых группах — писать нечего")
        return outcome

    api = service.api
    outcome.uptime_before = _uptime(api)

    changes = {plan.client.client_id: plan.target_tag for plan in effective}
    emails = {plan.client.client_id: plan.client.email for plan in effective}
    stats = service.store.apply_changes(changes, emails, [], now=None)
    outcome.db_changed = any(bool(v) for v in stats.values())
    outcome.moved = [
        (plan.client.email, plan.current_tag or "—", plan.target_tag) for plan in effective
    ]

    report = service.sync()
    outcome.sync_report = report
    outcome.config_written = report.config_written
    outcome.routing_planned_change = report.routing_planned_change
    outcome.errors.extend(report.errors)
    outcome.warnings.extend(report.warnings)

    outcome.uptime_after = _uptime(api)
    outcome.live_status = service.live_balancer_state()

    if verify:
        for plan in effective:
            ok, detail = service.verify_client_route(plan.client.email, [plan.target_tag])
            outcome.route_checks.append((plan.client.email, ok, detail))
            if ok is False:
                outcome.errors.append(f"{plan.client.email}: маршрут не подтверждён — {detail}")
    return outcome
