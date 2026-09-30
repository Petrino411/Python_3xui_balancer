"""Синхронизация состояния: клиенты панели -> sticky-назначения -> managed routing.

Здесь собраны все защиты ТЗ:
  * §9  — правки только после свежего чтения, с проверкой, что чужая часть конфига не потеряна;
  * §10 — кандидат валидируется локально (и, опционально, `xray -test`) до записи;
  * §24 — при отсутствии изменений нет ни записи, ни смены таймстемпов;
  * §31 — API-ошибки не роняют сервис, состояние сохраняется;
  * §32 — невалидный/пустой ответ API никогда не трактуется как «клиентов 0»;
  * §33 — обновление конфига атомарно, старый конфиг остаётся рабочим при любой ошибке;
  * §34 — backup перед фактическим изменением с ротацией.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__, routing
from .allocator import build_plan
from .api import PanelApi, PanelError, PanelResponseError
from .config import AppConfig
from .database import StateStore
from .models import Assignment, BalancerSpec, PanelClient, SyncPlan

logger = logging.getLogger(__name__)

META_LAST_SYNC = "last_successful_sync"
META_LAST_CONFIG_UPDATE = "last_config_update"
META_LAST_ERROR = "last_error"
META_REPAIR_STREAK = "config_repair_streak"
META_WRITE_FAILURES = "write_failure_streak"
META_VALIDATION_SIGNATURE = "last_validation_problem_signature"
META_CONFIG_WRITES = "config_writes_total"

CORE_NOT_READY_MARKERS = (
    "xray is not running",
    "connection refused",
    "code = Unavailable",
    "transport: Error while dialing",
)


def _is_core_not_ready(text: str) -> bool:
    """Ответ панели означает «ядро ещё стартует», а не ошибку конфига/маршрутизации (§37)."""
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in CORE_NOT_READY_MARKERS)


def specs_from_config(config: AppConfig) -> list[BalancerSpec]:
    return [
        BalancerSpec(
            tag=b.tag,
            primary=b.primary,
            fallback=config.fallback.outbound,
            strategy=b.strategy or "leastPing",
            strategy_settings=b.strategy_settings,
        )
        for b in config.balancers
    ]


@dataclass
class SyncReport:
    """Результат одного цикла: то, что печатают sync --dry-run и логи daemon."""

    dry_run: bool = False
    clients_total: int = 0
    distribution: dict[str, int] = field(default_factory=dict)
    new_assignments: dict[int, str] = field(default_factory=dict)
    new_emails: dict[int, str] = field(default_factory=dict)
    removed: list[tuple[int, str, str]] = field(default_factory=list)
    email_updates: list[tuple[int, str, str]] = field(default_factory=list)
    routing_planned_change: bool = False
    config_written: bool = False
    db_written: bool = False
    route_check_errors: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    repair_events: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_assignments
            or self.removed
            or self.email_updates
            or self.routing_planned_change
        )

    def distribution_text(self) -> str:
        return " ".join(f"{tag}={count}" for tag, count in self.distribution.items())

    def render(self, config: AppConfig) -> str:
        """Печатный отчёт (§22)."""
        lines: list[str] = []
        lines.append(f"Found clients: {self.clients_total}")
        if self.excluded:
            lines.append("")
            lines.append("Excluded (exclude_clients/exclude_regex):")
            for email in sorted(self.excluded):
                lines.append(f"  {email}")
        lines.append("")
        if self.new_assignments:
            lines.append("New:")
            for client_id in sorted(self.new_assignments):
                email = self.new_emails.get(client_id, f"client#{client_id}")
                lines.append(f"  {email} -> {self.new_assignments[client_id]}")
        else:
            lines.append("New: none")
        lines.append("")
        if self.removed:
            lines.append("Removed:")
            for client_id, email, tag in self.removed:
                lines.append(f"  {email} (id={client_id}, was {tag})")
        else:
            lines.append("Removed: none")
        if self.email_updates:
            lines.append("")
            lines.append("Renamed (same client_id, assignment kept):")
            for client_id, old, new in self.email_updates:
                lines.append(f"  {old} -> {new} (id={client_id})")
        lines.append("")
        lines.append("Changes to routing:")
        for tag, count in self.distribution.items():
            lines.append(f"  {tag}: {count}")
        lines.append("")
        if self.errors:
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"  {err}")
            lines.append("")
        for warning in self.warnings:
            lines.append(f"WARNING: {warning}")
        if self.warnings:
            lines.append("")
        if self.dry_run:
            lines.append("No changes applied. (dry-run)")
        else:
            if self.config_written:
                lines.append("Routing updated successfully.")
            elif self.routing_planned_change:
                lines.append("Routing NOT updated (see errors).")
            else:
                lines.append("No routing changes required")
            if self.db_written:
                lines.append("Local state updated.")
            if self.route_check_errors:
                lines.append("")
                lines.append("Route test problems:")
                for problem in self.route_check_errors:
                    lines.append(f"  {problem}")
        return "\n".join(lines)


class BackupStore:
    """Резервные копии шаблона конфига (§34): только при фактическом изменении + ротация."""

    def __init__(self, directory: str, keep: int, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.keep = keep
        self.enabled = enabled

    def save(self, template: Mapping[str, Any], note: str = "") -> Path | None:
        if not self.enabled:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.directory / f"xray-template-{stamp}.json"
        suffix = 1
        while path.exists():
            path = self.directory / f"xray-template-{stamp}-{suffix}.json"
            suffix += 1
        payload = {"saved_at": datetime.now().isoformat(timespec="seconds"), "note": note, "template": template}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rotate()
        logger.info("Backup конфига: %s", path)
        return path

    def _rotate(self) -> None:
        files = sorted(self.directory.glob("xray-template-*.json"))
        for old in files[: max(0, len(files) - self.keep)]:
            try:
                old.unlink()
            except OSError as exc:  # pragma: no cover - IO-ошибка
                logger.warning("Не удалось удалить старый backup %s: %s", old, exc)

    def latest(self) -> Path | None:
        files = sorted(self.directory.glob("xray-template-*.json"))
        return files[-1] if files else None


@dataclass
class WriteOutcome:
    written: bool
    repaired: bool = False
    problems: list[str] = field(default_factory=list)
    foreign_lost: list[str] = field(default_factory=list)


class BalancerService:
    """Оркестратор одного цикла синхронизации."""

    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        api: PanelApi,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        backups: BackupStore | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.api = api
        self.clock = clock
        self.sleep = sleep
        self.specs = specs_from_config(config)
        self.balancer_tags = [spec.tag for spec in self.specs]
        # outbound-теги, которые сервис добавляет в subjectSelector healthcheck-секции
        self.observatory_tags = sorted(
            {spec.primary for spec in self.specs} | {spec.fallback for spec in self.specs}
        )
        self.backups = backups or BackupStore(
            config.backups.directory, config.backups.keep, config.backups.enabled
        )

    # ------------------------------------------------------------------ helpers

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _spec_by_tag(self) -> dict[str, BalancerSpec]:
        return {spec.tag: spec for spec in self.specs}

    def _log_validation_problems(self, problems: Sequence[str], *, context: str) -> None:
        """Не спамить одинаковыми ошибками каждые 30 секунд."""
        signature = routing.digest(sorted(problems))
        previous = self.store.get_meta(META_VALIDATION_SIGNATURE)
        level = logging.DEBUG if previous == signature else logging.ERROR
        logger.log(level, "%s: %d проблем(ы) с конфигом, запись отменена", context, len(problems))
        if previous != signature:
            for problem in problems:
                logger.error("  %s", problem)
            self.store.set_meta(META_VALIDATION_SIGNATURE, signature)

    def _clear_validation_signature(self) -> None:
        if self.store.get_meta(META_VALIDATION_SIGNATURE):
            self.store.set_meta(META_VALIDATION_SIGNATURE, "")

    def _clients(self) -> list[PanelClient]:
        """Получить подтверждённый список клиентов (§32)."""
        clients = self.api.get_clients()
        return [
            PanelClient(
                client_id=c.client_id,
                email=c.email,
                enable=c.enable,
                inbound_ids=c.inbound_ids,
                uuid=c.uuid,
                sub_id=c.sub_id,
                total_bytes=c.total_bytes,
                up=c.up,
                down=c.down,
                expiry_time_ms=c.expiry_time_ms,
                now_ms=self._now_ms(),
            )
            for c in clients
        ]

    def fetch_clients(self) -> list[PanelClient]:
        """Публичный доступ к списку клиентов с проставленным «сейчас» (для CLI-команд)."""
        return self._clients()

    def _effective_groups(self, plan: SyncPlan) -> dict[str, list[str]]:
        return {tag: list(plan.groups.get(tag, [])) for tag in self.balancer_tags}

    def build_candidate(self, template: Mapping[str, Any], groups: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        """Собрать кандидат: наши правила + наши балансировщики + healthcheck."""
        block = routing.managed_block(groups, self.specs, self.config.routing)
        candidate = routing.apply_managed_block(template, block, self.config.routing, self.balancer_tags)
        candidate = routing.apply_balancers(candidate, self.specs)
        candidate, _ = routing.apply_observatory(candidate, self.specs, self.config.observatory)
        return candidate

    # ------------------------------------------------------------------ main cycle

    def sync(self, *, dry_run: bool = False, force_write: bool = False) -> SyncReport:
        report = SyncReport(dry_run=dry_run)
        logger.info("Sync started")

        assignments = self.store.load_assignments()
        log_assignments = {cid: a for cid, a in assignments.items()}

        try:
            clients = self._clients()
        except PanelError as exc:
            report.errors.append(str(exc))
            logger.error("Unable to fetch clients: %s", exc)
            logger.warning("Keeping previous state and routing unchanged")
            self.store.set_meta(META_LAST_ERROR, f"{datetime.now().isoformat(timespec='seconds')} {exc}")
            return report

        report.clients_total = len(clients)
        logger.info("Received %d clients", len(clients))

        if not clients and assignments and self.config.safety.refuse_empty_panel:
            message = (
                f"панель вернула пустой список клиентов, а в локальной БД {len(assignments)} назначений: "
                "считаю это ошибкой API, состояние и routing не меняю (§32)"
            )
            report.errors.append(message)
            logger.error("%s", message)
            logger.warning("Keeping previous state and routing unchanged")
            self.store.set_meta(META_LAST_ERROR, f"{datetime.now().isoformat(timespec='seconds')} {message}")
            return report

        plan = build_plan(
            log_assignments,
            clients,
            self.specs,
            include_disabled=self.config.include_disabled,
            is_excluded=self.config.is_excluded,
        )
        groups = self._effective_groups(plan)
        report.distribution = plan.counts
        report.new_assignments = dict(plan.new_assignments)
        report.new_emails = {
            cid: next((c.email for c in clients if c.client_id == cid), "") for cid in plan.new_assignments
        }
        report.removed = [
            (cid, assignments[cid].email, assignments[cid].balancer_tag)
            for cid in plan.removed_client_ids
            if cid in assignments
        ]
        report.email_updates = [
            (cid, assignments[cid].email, new_email)
            for cid, new_email in sorted(plan.email_updates.items())
            if cid in assignments
        ]
        report.excluded = plan.excluded_emails

        for client_id, tag in sorted(plan.new_assignments.items()):
            email = report.new_emails.get(client_id, str(client_id))
            logger.info("New client %s", email)
            logger.info("Assigned %s -> %s", email, tag)
        for client_id, email, _tag in report.removed:
            logger.info("Removed client %s from local state", email)

        # ------- routing
        try:
            template = self.api.get_xray_template()
        except PanelError as exc:
            report.errors.append(str(exc))
            logger.error("Unable to fetch xray config: %s", exc)
            logger.warning("Keeping previous state and routing unchanged")
            self.store.set_meta(META_LAST_ERROR, f"{datetime.now().isoformat(timespec='seconds')} {exc}")
            return report

        current_block = routing.describe_managed_block(template, self.balancer_tags)
        if current_block:
            logger.debug("Текущий блок сервиса в конфиге: %s", current_block)

        candidate = self.build_candidate(template, groups)
        report.routing_planned_change = routing.template_changed(template, candidate)

        # проверка «чужое не потеряно» нашим же merge
        if routing.foreign_signature(
            template, self.balancer_tags, self.observatory_tags
        ) != routing.foreign_signature(candidate, self.balancer_tags, self.observatory_tags):  # pragma: no cover - внутренняя защита
            message = "merge изменил чужую часть конфига — запись отменена"
            report.errors.append(message)
            logger.critical("%s", message)
            return report

        problems = routing.validate_candidate(
            candidate, groups, self.specs, known_outbound_tags=self._known_outbound_tags()
        )
        if problems:
            self._log_validation_problems(problems, context="Кандидат конфига невалиден")
            report.errors.extend(problems)
            report.routing_planned_change = True

        if not problems:
            self._clear_validation_signature()

        if not problems and not report.routing_planned_change and not report.has_changes:
            logger.info(
                "No changes. %d clients, distribution %s", report.clients_total, report.distribution_text()
            )
            if not dry_run:
                self.store.set_meta(META_LAST_SYNC, str(int(self.clock())))
                self.store.set_meta(META_LAST_ERROR, "")
            return report

        if dry_run:
            logger.info("dry-run: изменения не применяются")
            return report

        # ------- применяем локальные изменения: sticky-назначение фиксируется даже если
        # конфиг сейчас записать нельзя (например не создан нужный outbound) — иначе
        # решение о группе принималось бы заново на каждом цикле.
        state_emails = dict(plan.email_updates)
        for client_id in plan.new_assignments:
            state_emails.setdefault(client_id, report.new_emails.get(client_id, ""))
        stats = self.store.apply_changes(
            plan.new_assignments, state_emails, plan.removed_client_ids, now=int(self.clock())
        )
        report.db_written = any(stats.values())

        if problems:
            report.errors.extend(problems)
            self._log_validation_problems(problems, context="Кандидат конфига невалиден")
            logger.warning("Nothing applied, routing unchanged")
            self.store.set_meta(META_LAST_SYNC, str(int(self.clock())))
            self.store.set_meta(META_LAST_ERROR, report.errors[0])
            return report

        # ------- пишем конфиг (если он реально расходится)
        if report.routing_planned_change:
            outcome = self._write_config(template, groups, force_write=force_write)
            report.config_written = outcome.written
            report.repair_events = 1 if outcome.repaired else 0
            report.errors.extend(outcome.problems)
            if outcome.foreign_lost:
                report.warnings.extend(outcome.foreign_lost)
        else:
            logger.info("No routing changes required")

        self.store.set_meta(META_LAST_SYNC, str(int(self.clock())))
        if report.config_written:
            self.store.set_meta(META_LAST_CONFIG_UPDATE, str(int(self.clock())))
        if report.errors:
            self.store.set_meta(META_LAST_ERROR, report.errors[0])
        else:
            self.store.set_meta(META_LAST_ERROR, "")

        if report.config_written and self.config.validation.route_test:
            report.route_check_errors = self._verify_routes(groups)
        return report

    # ------------------------------------------------------------------ config write

    def _churn_breaker_active(self) -> bool:
        streak = int(self.store.get_meta(META_REPAIR_STREAK) or "0")
        return streak >= self.config.safety.churn_breaker_cycles

    def _write_config(
        self,
        previous_read: Mapping[str, Any],
        groups: Mapping[str, Sequence[str]],
        *,
        force_write: bool = False,
    ) -> WriteOutcome:
        """Прочитать заново, собрать кандидат, проверить, записать, подтвердить (§9, §33)."""
        if self._churn_breaker_active() and not force_write:
            message = (
                "сработал churn-предохранитель: конфиг переписывается слишком часто, "
                "автоматические изменения остановлены (проверьте routing руками, затем `sync --force-write`)"
            )
            logger.critical("%s", message)
            return WriteOutcome(written=False, problems=[message])

        # 1. актуальное чтение, чтобы не перезаписать чужие правки
        fresh = self.api.get_xray_template()
        if routing.foreign_signature(
            fresh, self.balancer_tags, self.observatory_tags
        ) != routing.foreign_signature(previous_read, self.balancer_tags, self.observatory_tags):
            message = "конфиг изменился между чтением и записью — цикл отменён, повтор на следующем тике"
            logger.warning("%s", message)
            return WriteOutcome(written=False, problems=[message])

        candidate = self.build_candidate(fresh, groups)
        foreign_before = routing.foreign_signature(
            candidate, self.balancer_tags, self.observatory_tags
        )

        if not routing.template_changed(fresh, candidate):
            logger.info("No routing changes required")
            return WriteOutcome(written=False)

        problems = routing.validate_candidate(
            candidate, groups, self.specs, known_outbound_tags=self._known_outbound_tags()
        )
        if problems:
            self._log_validation_problems(problems, context="Кандидат конфига невалиден")
            return WriteOutcome(written=False, problems=list(problems))

        if self.config.validation.local_xray_test:
            local_problems = self._local_xray_test(candidate, fresh)
            if local_problems:
                self._log_validation_problems(local_problems, context="xray -test отверг кандидата")
                return WriteOutcome(written=False, problems=local_problems)

        # 2. backup перед фактическим изменением
        backup = self.backups.save(fresh, note=f"before write v{__version__}")

        # 3. запись
        try:
            self.api.update_xray_template(candidate)
        except PanelError as exc:
            streak = self.store.bump_meta_counter(META_WRITE_FAILURES)
            logger.error("Unable to save xray config (attempt streak %d): %s", streak, exc)
            return WriteOutcome(written=False, problems=[f"панель не приняла конфиг: {exc}"])

        self.store.reset_meta_counter(META_WRITE_FAILURES)
        self.store.bump_meta_counter(META_CONFIG_WRITES)
        logger.info("Routing updated successfully")

        # 3b. панель перезапускает ядро на каждый принятый update — дожидаемся его и, если
        # ядро не поднялось, автоматически возвращаем предыдущий конфиг (§10, §33).
        if not self.api.wait_until_xray_ready(self.config.safety.xray_ready_timeout):
            state = {}
            try:
                state = self.api.xray_state()
            except PanelError:
                pass
            message = (
                "ядро Xray не поднялось после записи конфига "
                f"(state={state.get('state')!r}, errorMsg={state.get('errorMsg')!r}) — "
                "выполняется откат на предыдущий конфиг"
            )
            logger.critical("%s", message)
            rollback_problems = self._rollback(fresh, backup)
            return WriteOutcome(written=True, problems=[message, *rollback_problems])

        # 4. подтверждение: что записалось и не потерялось ли чужое
        return self._confirm_write(candidate, groups, foreign_before)

    def _rollback(self, previous: Mapping[str, Any], backup: Path | None) -> list[str]:
        """Вернуть предыдущий шаблон после неудачного применения."""
        problems: list[str] = []
        source = "из памяти"
        template = dict(previous)
        if backup is not None and backup.exists():
            try:
                template = json.loads(backup.read_text(encoding="utf-8"))
                source = f"из бэкапа {backup}"
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"бэкап {backup} не читается ({exc}), откат из памяти")
        try:
            self.api.update_xray_template(template)
        except PanelError as exc:
            problems.append(f"откат {source} не удался: {exc}")
            logger.critical("Rollback failed: %s", exc)
            return problems
        ready = self.api.wait_until_xray_ready(self.config.safety.xray_ready_timeout)
        logger.warning("Откат выполнен (%s), ядро поднялось: %s", source, ready)
        if not ready:
            problems.append("ядро не поднялось даже после отката — нужен разбор вручную")
        return problems

    def _confirm_write(
        self,
        candidate: Mapping[str, Any],
        groups: Mapping[str, Sequence[str]],
        foreign_before: str,
    ) -> WriteOutcome:
        try:
            post = self.api.get_xray_template()
        except PanelError as exc:
            message = f"конфиг записан, но подтверждение не получено: {exc}"
            logger.warning("%s", message)
            return WriteOutcome(written=True, problems=[message])

        diff = routing.foreign_diff(candidate, post, self.balancer_tags, self.observatory_tags)
        managed_ok = not routing.validate_candidate(
            post, groups, self.specs, known_outbound_tags=self._known_outbound_tags()
        )
        if diff.empty and managed_ok:
            self.store.reset_meta_counter(META_REPAIR_STREAK)
            return WriteOutcome(written=True)

        detail = (
            f"added rules={diff.added_rules[:5]}, removed rules={diff.removed_rules[:5]}, "
            f"sections={diff.sections_changed}"
        )

        if self.config.safety.config_repair_attempts <= 0:
            logger.critical("После записи конфиг расходится: %s", detail)
            return WriteOutcome(written=True, problems=[f"конфиг расходится после записи: {detail}"])

        # Чужая часть изменилась между чтением и записью (потерянное обновление) либо панель
        # трансформировала правила — повторяем merge уже от свежего состояния, ничего не откатывая.
        streak = self.store.bump_meta_counter(META_REPAIR_STREAK)
        logger.warning("После записи обнаружено расхождение (%s), повторный merge (попытка %d)", detail, streak)
        try:
            repaired_candidate = self.build_candidate(post, groups)
            self.api.update_xray_template(repaired_candidate)
        except PanelError as exc:
            logger.critical("Повторный merge не удался: %s", exc)
            return WriteOutcome(
                written=True, repaired=True, problems=[f"не удалось выровнять конфиг после записи: {exc}"]
            )

        try:
            final = self.api.get_xray_template()
        except PanelError as exc:
            return WriteOutcome(written=True, repaired=True, problems=[f"финальное чтение не удалось: {exc}"])

        final_diff = routing.foreign_diff(
            repaired_candidate, final, self.balancer_tags, self.observatory_tags
        )
        final_managed_ok = not routing.validate_candidate(final, groups, self.specs)
        if final_diff.empty and final_managed_ok:
            self.store.reset_meta_counter(META_REPAIR_STREAK)
            logger.info("Конфиг выровнен повторным merge")
            return WriteOutcome(written=True, repaired=True)

        logger.critical(
            "Конфиг расходится и после повторного merge: added=%s removed=%s sections=%s",
            final_diff.added_rules[:5],
            final_diff.removed_rules[:5],
            final_diff.sections_changed,
        )
        return WriteOutcome(
            written=True,
            repaired=True,
            problems=["конфиг расходится и после повторного merge — автоматические записи остановлены"],
            foreign_lost=[
                "часть пользовательских настроек routing могла быть перезаписана — "
                f"восстановите из backup: {self.backups.latest()}"
            ],
        )

    # ------------------------------------------------------------------ validation helpers

    def _local_xray_test(
        self, candidate: Mapping[str, Any], template: Mapping[str, Any]
    ) -> list[str]:
        """Собрать рабочий конфиг с нашим routing и прогнать `xray -test` (§10, §33)."""
        binary = self.config.validation.xray_binary
        if not Path(binary).exists():
            return [f"локальная проверка включена, но бинарник xray не найден: {binary}"]
        try:
            running = self.api.get_running_config()
        except PanelError as exc:
            return [f"локальная проверка невозможна: не удалось получить собранный конфиг: {exc}"]
        if not running:
            return ["локальная проверка невозможна: панель вернула пустой собранный конфиг"]
        candidate_runtime = dict(running)
        candidate_runtime["routing"] = candidate.get("routing")
        for key in ("observatory", "burstObservatory"):
            if key in candidate:
                candidate_runtime[key] = candidate[key]
            else:
                candidate_runtime.pop(key, None)
        fd, path = tempfile.mkstemp(prefix="xcb-candidate-", suffix=".json")
        try:
            with open(fd, "w", encoding="utf-8") as handle:
                json.dump(candidate_runtime, handle)
            proc = subprocess.run(
                [binary, "run", "-test", "-c", path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [f"xray -test не выполнился: {exc}"]
        finally:
            try:
                Path(path).unlink()
            except OSError:
                pass
        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr).strip().splitlines()
            return [f"xray -test rc={proc.returncode}: {line}" for line in output[-5:]]
        logger.info("xray -test: кандидат конфига валиден")
        return []

    def _known_outbound_tags(self) -> set[str]:
        """Outbound-теги, доступные ядру: шаблон + работающий конфиг + кэш.

        В 3.8.x outbound'ы могут появляться из подписок панели, а не из шаблона
        (на узле hub.main: sub1-mskserv, sub2-cringe, sub3-tls-riga2-tls,
        sub1-tls-stock-tls). Поэтому теги интерпретируются как валидные, если
        встречаются в собранном работающем конфиге.
        """
        tags: set[str] = set()
        try:
            section = self.api.get_running_config()
        except PanelError as exc:
            logger.warning("Не удалось прочитать работающий конфиг для проверки outbound-тегов: %s", exc)
            return tags
        for outbound in section.get("outbounds") or []:
            if isinstance(outbound, dict) and outbound.get("tag"):
                tags.add(str(outbound["tag"]))
        return tags

    def _inbound_tags(self) -> list[str]:
        """Теги inbound'ов из работающего конфига.

        Нужны для routeTest: на реальных конфигах правило может совпадать по inboundTag,
        и без inboundTag ядро отвечает matched=false даже при корректной маршрутизации
        (измерено на hub.main: правило balancer привязано к in-44749-tcp).
        """
        try:
            section = self.api.get_running_config()
        except PanelError:
            return []
        return [str(i.get("tag")) for i in (section.get("inbounds") or []) if isinstance(i, dict) and i.get("tag")]

    def _verify_routes(self, groups: Mapping[str, Sequence[str]]) -> list[str]:
        """§43: спросить у ядра по одному клиенту из каждой группы, куда он пойдёт.

        Ошибкой считаем ТОЛЬКО неверный маршрут. Если ядро в этот момент ещё поднимается
        (панель уже говорит «running», но grpc-api не слушает) — это «не проверено» (WARNING),
        а не ошибка маршрутизации (§37): подъём ядра и откат контролирует safety-слой.
        """
        problems: list[str] = []
        unverified: list[str] = []
        inbound_tags = self._inbound_tags()
        for spec in self.specs:
            emails = list(groups.get(spec.tag, []))
            if not emails:
                continue
            email = emails[0]
            result = None
            errors: list[str] = []
            core_busy = False
            for inbound_tag in [""] + inbound_tags:
                try:
                    result = self.api.route_test(
                        domain=self.config.validation.route_test_domain,
                        email=email,
                        network="tcp",
                        inbound_tag=inbound_tag,
                    )
                except PanelError as exc:
                    errors.append(str(exc))
                    if _is_core_not_ready(str(exc)):
                        core_busy = True
                        # ядро перезапускается: дождёмся готовности и повторим этот же тест
                        if self._wait_route_api():
                            try:
                                result = self.api.route_test(
                                    domain=self.config.validation.route_test_domain,
                                    email=email,
                                    network="tcp",
                                    inbound_tag=inbound_tag,
                                )
                                errors.clear()
                            except PanelError as retry_exc:
                                errors.append(str(retry_exc))
                    continue
                if result.matched:
                    break
            if result is None:
                if core_busy and self.api.xray_is_running():
                    unverified.append(f"{email} ({spec.tag}): ядро ещё поднимается, маршрут не проверен")
                else:
                    problems.append(f"{email}: routeTest недоступен: {errors[:1]}")
                continue
            expected_ok = result.matched and (
                spec.tag in result.group_tags or result.outbound_tag in (spec.primary, spec.fallback)
            )
            if not expected_ok:
                problems.append(
                    f"{email} должен идти через {spec.tag} -> {spec.primary}, "
                    f"а ядро вернуло outboundTag={result.outbound_tag!r} groupTags={result.group_tags} matched={result.matched}"
                )
        for item in unverified:
            logger.warning("route test: %s", item)
        for problem in problems:
            logger.critical("route test: %s", problem)
        if not problems and not unverified:
            logger.info("Route test: все группы маршрутизируются ожидаемо")
        return problems

    def verify_client_route(
        self, email: str, expected_tags: Sequence[str]
    ) -> tuple[bool | None, str]:
        """Куда ядро отправит конкретного клиента: (True|False|None, пояснение).

        True/False — ядро ответило и маршрут совпал/не совпал с ожиданием.
        None — ответа не получили (ядро ещё поднимается после перезапуска панелью):
        это «не проверено» (WARNING), а не ошибка маршрутизации (§37).
        """
        allowed = set(expected_tags)
        spec = next((s for s in self.specs if s.tag in allowed), None)
        if spec is not None:
            allowed |= {spec.primary, spec.fallback}

        errors: list[str] = []
        core_busy = False
        for inbound_tag in [""] + self._inbound_tags():
            for attempt in range(2):
                try:
                    result = self.api.route_test(
                        domain=self.config.validation.route_test_domain,
                        email=email,
                        network="tcp",
                        inbound_tag=inbound_tag,
                    )
                except PanelError as exc:
                    errors.append(str(exc))
                    if not _is_core_not_ready(str(exc)):
                        break
                    core_busy = True
                    if attempt == 0 and self._wait_route_api():
                        continue  # ядро поднялось — повторить тот же inboundTag
                    break
                if result.matched:
                    ok = result.outbound_tag in allowed or bool(allowed & set(result.group_tags))
                    detail = (
                        f"inboundTag={inbound_tag or '—'} outboundTag={result.outbound_tag or '—'} "
                        f"groupTags={result.group_tags}"
                    )
                    return ok, detail
                break  # правило не совпало по этому inboundTag — пробуем следующий
        if core_busy:
            return None, f"ядро ещё поднимается после записи конфига: {errors[:1]}"
        return False, f"routeTest не подтвердил маршрут: {errors[:1] or 'matched=false'}"

    def _wait_route_api(self, timeout: float | None = None) -> bool:
        """Готовность ядра к routeTest: состояние «running» + фактический ответ grpc-api."""
        if timeout is None:
            timeout = self.config.safety.xray_ready_timeout
        delay = 2.0
        attempts = max(2, int(timeout / delay) + 1)
        for attempt in range(attempts):
            if self.api.xray_is_running():
                try:
                    self.api.balancer_status(self.balancer_tags)
                    return True
                except PanelError as exc:
                    if not _is_core_not_ready(str(exc)):
                        return True
            if attempt + 1 < attempts:
                self.sleep(delay)
        return self.api.xray_is_running()

    # ------------------------------------------------------------------ live status

    def live_balancer_state(self) -> dict[str, Any]:
        try:
            return self.api.balancer_status(self.balancer_tags)
        except PanelError as exc:
            logger.warning("balancerStatus недоступен: %s", exc)
            return {}


def distribution_of(assignments: Mapping[int, Assignment], tags: Sequence[str]) -> dict[str, int]:
    counts = {tag: 0 for tag in tags}
    for assignment in assignments.values():
        if assignment.balancer_tag in counts:
            counts[assignment.balancer_tag] += 1
    return counts


def find_config_backup(directory: str) -> Path | None:
    files = sorted(Path(directory).glob("xray-template-*.json"))
    return files[-1] if files else None


def copy_for_inspection(path: Path, destination: Path) -> None:  # pragma: no cover - утилита
    shutil.copy2(path, destination)
