"""Модели данных сервиса: клиент панели, назначение, балансировщик, план синхронизации."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

BALANCER_TAG_PREFIX = "client-balancer-"


@dataclass(frozen=True)
class PanelClient:
    """Один логический клиент панели (ключ — внутренний ID записи, а не email)."""

    client_id: int
    email: str
    enable: bool = True
    inbound_ids: tuple[int, ...] = ()
    uuid: str = ""
    sub_id: str = ""
    total_bytes: int = 0
    up: int = 0
    down: int = 0
    expiry_time_ms: int = 0
    now_ms: int = 0

    @property
    def expired(self) -> bool:
        if self.expiry_time_ms <= 0 or self.now_ms <= 0:
            return False
        return self.expiry_time_ms < self.now_ms

    @property
    def traffic_exhausted(self) -> bool:
        if self.total_bytes <= 0:
            return False
        return (self.up + self.down) >= self.total_bytes

    @property
    def active(self) -> bool:
        """Клиент фактически обслуживается Xray (не disabled/expired/закончился трафик)."""
        return self.enable and not self.expired and not self.traffic_exhausted

    def masked(self) -> dict[str, Any]:
        """Представление для логов/status без секретов (UUID не печатаем)."""
        return {
            "client_id": self.client_id,
            "email": self.email,
            "enable": self.enable,
            "active": self.active,
            "inbounds": list(self.inbound_ids),
            "uuid": (self.uuid[:4] + "…") if self.uuid else "",
            "sub_id": (self.sub_id[:4] + "…") if self.sub_id else "",
        }


@dataclass(frozen=True)
class Assignment:
    """Sticky-назначение клиента на балансировщик."""

    client_id: int
    email: str
    balancer_tag: str
    created_at: int
    updated_at: int

    def as_row(self) -> tuple[int, str, str, int, int]:
        return (self.client_id, self.email, self.balancer_tag, self.created_at, self.updated_at)


@dataclass(frozen=True)
class BalancerSpec:
    """Описание одного балансировщика: один primary, общий fallback."""

    tag: str
    primary: str
    fallback: str
    strategy: str = "leastPing"
    strategy_settings: dict[str, Any] | None = None

    @property
    def selector(self) -> list[str]:
        """В selector ровно один outbound — иначе Xray начнёт выбирать outbound на соединение."""
        return [self.primary]


@dataclass
class SyncPlan:
    """Результат reconcile: что нужно сделать с БД и с routing."""

    groups: dict[str, list[str]] = field(default_factory=dict)  # tag -> emails (сортировано)
    new_assignments: dict[int, str] = field(default_factory=dict)  # client_id -> tag
    removed_client_ids: list[int] = field(default_factory=list)
    email_updates: dict[int, str] = field(default_factory=dict)  # client_id -> новый email
    excluded_emails: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # детерминированный порядок тегов

    @property
    def counts(self) -> dict[str, int]:
        return {tag: len(self.groups.get(tag, [])) for tag in self.order}

    @property
    def has_db_changes(self) -> bool:
        return bool(self.new_assignments or self.removed_client_ids or self.email_updates)

    @property
    def distribution(self) -> str:
        counts = self.counts
        return " ".join(f"{tag.split('client-balancer-')[-1]}:{counts[tag]}" for tag in self.order)

    def summary(self) -> str:
        counts = self.counts
        return "/".join(str(counts[tag]) for tag in self.order)

    @property
    def total_clients(self) -> int:
        return sum(len(v) for v in self.groups.values())


def group_assignments(
    assignments: Iterable[Assignment], order: Iterable[str]
) -> dict[str, list[str]]:
    """Сгруппировать назначения по балансировщикам, сохранив заданный порядок тегов.

    Email внутри группы уникальны: две записи панели с одинаковым email не должны
    превратиться в два одинаковых элемента одного правила `user`.
    """
    groups: dict[str, list[str]] = {tag: [] for tag in order}
    seen: dict[str, set[str]] = {tag: set() for tag in order}
    for a in assignments:
        bucket = groups.setdefault(a.balancer_tag, [])
        known = seen.setdefault(a.balancer_tag, set())
        if a.email in known:
            continue
        known.add(a.email)
        bucket.append(a.email)
    for tag in list(groups):
        groups[tag] = sort_emails(groups[tag])
    return groups


def sort_emails(emails: Iterable[str]) -> list[str]:
    """Стабильный порядок пользователей внутри правила (§28): по email без учёта регистра."""
    return sorted(emails, key=lambda e: (e.lower(), e))
