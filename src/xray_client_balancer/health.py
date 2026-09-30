"""Наблюдаемость: сколько памяти занимает процесс и как растут файлы на диске.

Зачем это отдельным модулем: демон работает месяцами в цикле раз в 30 секунд, поэтому
«не течёт ли память» и «не забьёт ли он диск» — это утверждения, которые нужно
проверять замерами, а не на глаз. Здесь собраны измерения, которые:

  * печатает `xcb doctor` в любой момент (память процесса, размер state.db и WAL,
    размер и количество бэкапов, свободное место на их файловых системах);
  * пишет в журнал сам демон раз в час, чтобы рост был виден в journalctl без
    запуска отдельных инструментов.

Ничего не изменяет: только читает /proc, размеры файлов и `shutil.disk_usage`.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import AppConfig

logger = logging.getLogger(__name__)

MIB = 1024 * 1024

# Пороги для WARNING. Подобраны с запасом: на узел-эталон (31 клиент) сервис пишет
# state.db десятки килобайт, WAL самоограничивается примерно 4 МБ (SQLite
# checkpoint по умолчанию при 1000 страниц), бэкапы ротируются по backups.keep.
WAL_WARN_BYTES = 32 * MIB
STATE_WARN_BYTES = 64 * MIB
BACKUPS_WARN_BYTES = 256 * MIB
DISK_FREE_WARN_BYTES = 1024 * MIB
DISK_USED_WARN_PERCENT = 90.0
RSS_WARN_BYTES = 256 * MIB
FD_WARN = 256


# --------------------------------------------------------------------- процесс


@dataclass(frozen=True)
class ProcessMetrics:
    """Что можно узнать про процесс по /proc (Linux)."""

    pid: int
    rss_bytes: int
    vms_bytes: int
    threads: int
    open_fds: int | None
    uptime_seconds: float | None
    cmdline: str

    def human_rss(self) -> str:
        return f"{self.rss_bytes / MIB:.1f} MiB"


def _kv_from_status(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _first_int(value: str | None) -> int:
    if not value:
        return 0
    token = value.split()[0]
    try:
        return int(token)
    except ValueError:
        return 0


def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def _process_uptime(pid: int) -> float | None:
    """Время работы процесса: старт из /proc/<pid>/stat (22-е поле) + btime."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm может содержать пробелы и скобки — отрезаем его от хвоста строки
    tail = raw[raw.rfind(")") + 2 :].split()
    if len(tail) < 20:
        return None
    try:
        start_ticks = int(tail[19])
    except ValueError:
        return None
    ticks = os.sysconf("SC_CLK_TCK") or 100
    boot = _boot_time()
    if not boot:
        return None
    return max(0.0, time.time() - (boot + start_ticks / ticks))


def read_process_metrics(pid: int | None = None) -> ProcessMetrics | None:
    """Метрики процесса: по умолчанию — собственного, либо указанного (демон)."""
    target = os.getpid() if pid is None else int(pid)
    try:
        status = Path(f"/proc/{target}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = _kv_from_status(status)
    try:
        open_fds: int | None = len(os.listdir(f"/proc/{target}/fd"))
    except OSError:
        open_fds = None
    try:
        cmdline = (
            Path(f"/proc/{target}/cmdline")
            .read_bytes()
            .replace(b"\x00", b" ")
            .decode("utf-8", "replace")
            .strip()
        )
    except OSError:
        cmdline = ""
    return ProcessMetrics(
        pid=target,
        rss_bytes=_first_int(fields.get("VmRSS")) * 1024,
        vms_bytes=_first_int(fields.get("VmSize")) * 1024,
        threads=_first_int(fields.get("Threads")),
        open_fds=open_fds,
        uptime_seconds=_process_uptime(target),
        cmdline=cmdline[:200],
    )


# --------------------------------------------------------------------- файлы


@dataclass(frozen=True)
class PathUsage:
    path: str
    exists: bool
    bytes_total: int
    files: int


def path_usage(path: str | Path) -> PathUsage:
    """Размер каталога (рекурсивно) или одного файла."""
    target = Path(path)
    if not target.exists():
        return PathUsage(str(target), False, 0, 0)
    if target.is_file():
        try:
            return PathUsage(str(target), True, target.stat().st_size, 1)
        except OSError:
            return PathUsage(str(target), True, 0, 1)
    total = 0
    count = 0
    for item in target.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
                count += 1
        except OSError:  # pragma: no cover - файл исчез между scandir и stat
            continue
    return PathUsage(str(target), True, total, count)


@dataclass(frozen=True)
class FsUsage:
    path: str
    total: int
    used: int
    free: int
    percent: float


def filesystem_usage(path: str | Path) -> FsUsage | None:
    target = Path(path)
    probe = target if target.exists() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return FsUsage(
        path=str(probe),
        total=usage.total,
        used=usage.used,
        free=usage.free,
        percent=100.0 * usage.used / usage.total if usage.total else 0.0,
    )


def state_files(database: str) -> dict[str, PathUsage]:
    """Файлы SQLite-состояния: сама БД, журнал WAL и shared-memory индекс."""
    return {
        suffix: path_usage(f"{database}{suffix}") for suffix in ("", "-wal", "-shm")
    }


@dataclass(frozen=True)
class SqliteStats:
    """Статистика по файлу БД (открывается read-only, ничего не меняет)."""

    page_size: int
    page_count: int
    freelist_count: int

    @property
    def live_bytes(self) -> int:
        """Оценка «живых» данных: без страниц, лежащих в списке свободных."""
        return self.page_size * (self.page_count - self.freelist_count)


def sqlite_stats(database: str) -> SqliteStats | None:
    """Страницы БД: отличать «файл вырос из-за свободных страниц» от роста данных."""
    import sqlite3

    if not Path(database).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:  # pragma: no cover - БД занята/битая
        return None
    try:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except sqlite3.Error:  # pragma: no cover
        return None
    finally:
        conn.close()
    return SqliteStats(page_size=page_size, page_count=page_count, freelist_count=freelist)


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


# --------------------------------------------------------------------- отчёт


@dataclass
class Finding:
    """Одна строка отчёта: имя, статус (True/False/None = не проверено) и значение."""

    name: str
    ok: bool | None
    value: str
    note: str = ""
    details: list[str] = field(default_factory=list)

    def marker(self) -> str:
        if self.ok is None:
            return "SKIP"
        return "OK  " if self.ok else "WARN"


def collect_findings(config: AppConfig, *, pid: int | None = None, rss_warn: int = RSS_WARN_BYTES) -> list[Finding]:
    """Собрать все измерения. Ничего не меняет, ничего не блокирует."""
    findings: list[Finding] = []

    # 1. память процесса
    metrics = read_process_metrics(pid)
    if metrics is None:
        findings.append(
            Finding(
                f"процесс{' pid=' + str(pid) if pid else ' (текущий)'}",
                None,
                "не читается /proc",
                "для демона передайте --pid (см. systemctl show -p MainPID --value xray-client-balancer)",
            )
        )
    else:
        uptime = (
            f", работает {metrics.uptime_seconds / 3600:.1f} ч"
            if metrics.uptime_seconds is not None
            else ""
        )
        details: list[str] = []
        if metrics.cmdline:
            details.append(f"cmdline: {metrics.cmdline}")
            if "xray" not in metrics.cmdline.lower():
                details.append("похоже, это не процесс сервиса — проверьте --pid (см. cmdline)")
        findings.append(
            Finding(
                f"память процесса pid={metrics.pid}",
                None if metrics.rss_bytes == 0 else metrics.rss_bytes < rss_warn,
                f"RSS {human_bytes(metrics.rss_bytes)} (virt {human_bytes(metrics.vms_bytes)})"
                f"{uptime}",
                f"порог WARNING {human_bytes(rss_warn)}",
                details,
            )
        )
        if metrics.open_fds is not None:
            findings.append(
                Finding(
                    "открытые файловые дескрипторы",
                    metrics.open_fds < FD_WARN,
                    str(metrics.open_fds),
                    f"порог WARNING {FD_WARN}; потоки: {metrics.threads}",
                )
            )

    # 2. файлы состояния
    files = state_files(config.state.database)
    db = files[""]
    wal = files["-wal"]
    shm = files["-shm"]
    total_state = db.bytes_total + wal.bytes_total + shm.bytes_total
    stats = sqlite_stats(config.state.database)
    details = [
        f"путь: {config.state.database}",
        f"файлы: БД {human_bytes(db.bytes_total)}, WAL {human_bytes(wal.bytes_total)}, "
        f"SHM {human_bytes(shm.bytes_total)}",
    ]
    if stats is not None:
        details.append(
            f"страниц {stats.page_count} по {stats.page_size} B, свободных {stats.freelist_count} "
            f"(данные ≈ {human_bytes(stats.live_bytes)})"
        )
    findings.append(
        Finding(
            "state.db",
            total_state < STATE_WARN_BYTES,
            f"всего {human_bytes(total_state)} (БД {human_bytes(db.bytes_total)}, "
            f"WAL {human_bytes(wal.bytes_total)})",
            f"порог WARNING {human_bytes(STATE_WARN_BYTES)} на всё вместе",
            details,
        )
    )
    if wal.bytes_total >= WAL_WARN_BYTES:
        findings.append(
            Finding(
                "WAL не сбрасывается",
                False,
                human_bytes(wal.bytes_total),
                "журнал WAL разросся: обычно значит, что checkpoint не проходит (долгая "
                "транзакция или второй процесс держит БД)",
            )
        )

    # 3. бэкапы
    backups = path_usage(config.backups.directory) if config.backups.enabled else PathUsage(
        config.backups.directory, False, 0, 0
    )
    if not config.backups.enabled:
        findings.append(Finding("бэкапы шаблона", None, "выключены в конфиге (backups.enabled=false)"))
    else:
        findings.append(
            Finding(
                "бэкапы шаблона",
                backups.bytes_total < BACKUPS_WARN_BYTES,
                f"{human_bytes(backups.bytes_total)} в {backups.files} файлах",
                f"ротация хранит backups.keep={config.backups.keep}; порог WARNING {human_bytes(BACKUPS_WARN_BYTES)}",
                [f"каталог: {config.backups.directory}"],
            )
        )
        if backups.files > config.backups.keep:
            findings.append(
                Finding(
                    "ротация бэкапов",
                    False,
                    f"файлов {backups.files} при backups.keep={config.backups.keep}",
                    "проверьте права на каталог: старые файлы не удаляются",
                )
            )

    # 4. свободное место на файловых системах, куда сервис пишет
    for label, path in (
        ("ФС состояния", config.state.database),
        ("ФС бэкапов", config.backups.directory),
    ):
        usage = filesystem_usage(path)
        if usage is None:
            findings.append(Finding(label, None, f"{path}: не удалось прочитать статистику"))
            continue
        ok = usage.free >= DISK_FREE_WARN_BYTES and usage.percent < DISK_USED_WARN_PERCENT
        findings.append(
            Finding(
                label,
                ok,
                f"{usage.path}: свободно {human_bytes(usage.free)} из {human_bytes(usage.total)} "
                f"({usage.percent:.0f}% занято)",
                f"пороги WARNING: свободно < {human_bytes(DISK_FREE_WARN_BYTES)} или занято > "
                f"{DISK_USED_WARN_PERCENT:.0f}%",
            )
        )

    # 5. временные файлы от локального `xray -test` (их сервис удаляет сразу после проверки)
    stray = _stray_temp_files(config)
    findings.append(
        Finding(
            "незакрытые временные файлы",
            not stray,
            f"{len(stray)} шт.",
            "сервис пишет кандидата как xcb-candidate-*.json и удаляет его сразу после "
            "`xray -test`; залежавшийся файл означает прерванный прогон",
            [f"  {p}" for p in stray[:5]],
        )
    )
    return findings


def _stray_temp_files(config: AppConfig) -> list[str]:
    """Файлы-кандидаты от `xray -test`, оставшиеся в каталоге состояния.

    Смотрим только собственный префикс: любые другие файлы рядом с БД — это дело
    администратора, и предупреждать о них нельзя (иначе doctor будет ругаться на
    соседний config.yaml и шуметь в мониторинге).
    """
    directory = Path(config.state.database).parent
    if not directory.exists():
        return []
    try:
        return sorted(str(p) for p in directory.glob("xcb-candidate-*"))
    except OSError:  # pragma: no cover
        return []


def render_findings(findings: Iterable[Finding]) -> str:
    lines: list[str] = []
    problems = 0
    for finding in findings:
        if finding.ok is False:
            problems += 1
        lines.append(f"[{finding.marker()}] {finding.name}: {finding.value}")
        if finding.note:
            lines.append(f"        {finding.note}")
        for detail in finding.details:
            lines.append(f"        {detail}")
    lines.append("")
    if problems:
        lines.append(f"ИТОГО: {problems} предупреждений (сервис продолжает работать)")
    else:
        lines.append("ИТОГО: все проверки памяти и диска в норме")
    return "\n".join(lines)


def resource_summary(config: AppConfig, pid: int | None = None) -> str:
    """Компактная строка для журнала демона: память процесса + рост файлов на диске."""
    metrics = read_process_metrics(pid)
    rss = human_bytes(metrics.rss_bytes) if metrics else "n/a"
    fds = metrics.open_fds if metrics and metrics.open_fds is not None else "n/a"
    state = path_usage(config.state.database)
    wal = path_usage(f"{config.state.database}-wal")
    backups = path_usage(config.backups.directory) if config.backups.enabled else None
    usage = filesystem_usage(config.state.database)
    parts = [
        f"rss={rss}",
        f"fds={fds}",
        f"state={human_bytes(state.bytes_total)}",
        f"wal={human_bytes(wal.bytes_total)}",
    ]
    if backups is not None:
        parts.append(f"backups={human_bytes(backups.bytes_total)}/{backups.files}")
    if usage is not None:
        parts.append(f"fs_free={human_bytes(usage.free)}")
    return " ".join(parts)
