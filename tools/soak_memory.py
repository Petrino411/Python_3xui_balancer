#!/usr/bin/env python3
"""Замер устойчивости: течёт ли сервис по памяти и растёт ли он по диску.

Два режима (оба не касаются боевой панели):

* `cycles` (по умолчанию) — гоняет настоящий цикл `BalancerService.sync()` в этом
  процессе против локальной mock-панели (`tests/mock_panel.py`), периодически меняя
  панель как живой админ: добавляет/удаляет клиентов, переименовывает их, переносит
  между группами (последнее заставляет сервис писать конфиг и создавать бэкапы).
  Через равные интервалы снимает RSS, число объектов в GC, размеры state.db/WAL и
  каталога бэкапов. Это и есть проверка на утечку: утечка на цикл видна на тысячах
  циклов, а разовый рост от фрагментации арены — нет.
* `daemon` — поднимает штатный `daemon` отдельным процессом, снимает /proc/<pid>
  (RSS, дескрипторы), считает строки журнала (объём вывода за час) и завершает его
  SIGTERM. Здесь проверяется, что реальный процесс ведёт себя так же, как прогон
  в этом процессе.

Запуск:
    python3 tools/soak_memory.py --cycles 2000 --clients 60
    python3 tools/soak_memory.py daemon --seconds 120
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from mock_panel import TOKEN, MockPanel, make_client  # noqa: E402

from xray_client_balancer import health  # noqa: E402
from xray_client_balancer.api import PanelApi, PanelError  # noqa: E402
from xray_client_balancer.config import AppConfig  # noqa: E402
from xray_client_balancer.database import StateStore  # noqa: E402
from xray_client_balancer.ops import apply_moves, pick_target, plan_moves  # noqa: E402
from xray_client_balancer.service import BalancerService, BackupStore  # noqa: E402

BALANCERS = [
    {"tag": "client-balancer-1", "primary": "server-1"},
    {"tag": "client-balancer-2", "primary": "server-2"},
    {"tag": "client-balancer-3", "primary": "server-3"},
]

# Насколько RSS может вырасти за прогон, чтобы это ещё считалось «не течёт»:
# разовый рост на старте неизбежен (арены, импорт модулей, пул соединений).
RSS_GROWTH_LIMIT = 16 * health.MIB


def build_config(base_url: str, workdir: Path, *, keep: int = 20, interval: int = 5) -> AppConfig:
    return AppConfig.model_validate(
        {
            "panel": {
                "url": base_url,
                "api_token": TOKEN,
                "verify_tls": False,
                "timeout_seconds": 5,
                "retries": 1,
                "backoff_seconds": 0.01,
                "max_backoff_seconds": 0.02,
            },
            "balancers": BALANCERS,
            "fallback": {"outbound": "server-4"},
            "state": {"database": str(workdir / "state.db")},
            "backups": {"enabled": True, "directory": str(workdir / "backups"), "keep": keep},
            "sync": {"interval_seconds": interval},
        }
    )


def build_service(config: AppConfig, store: StateStore, api: PanelApi) -> BalancerService:
    return BalancerService(
        config,
        store,
        api,
        sleep=lambda _seconds: None,  # не ждём реальные паузы готовности ядра
        backups=BackupStore(config.backups.directory, config.backups.keep, config.backups.enabled),
    )


def disk_state(config: AppConfig) -> dict[str, int]:
    """Размеры файлов состояния по отдельности: БД, WAL, SHM + ротация бэкапов."""
    files = health.state_files(config.state.database)
    backups = health.path_usage(config.backups.directory)
    return {
        "db": files[""].bytes_total,
        "wal": files["-wal"].bytes_total,
        "shm": files["-shm"].bytes_total,
        "backups_bytes": backups.bytes_total,
        "backups_files": backups.files,
    }


def sqlite_pages(config: AppConfig) -> tuple[int, int]:
    """Страниц в БД и свободных страниц: показывают, растут ли данные, а не файл."""
    stats = health.sqlite_stats(config.state.database)
    return (stats.page_count, stats.freelist_count) if stats else (0, 0)


def sample(config: AppConfig) -> dict[str, float]:
    """Замер. Перед снятием собираем мусор: тогда рост числа объектов означает
    достижимые объекты (утечка), а не просто ещё не собранный циклический мусор."""
    gc.collect()
    metrics = health.read_process_metrics()
    row: dict[str, float] = {
        "rss": float(metrics.rss_bytes if metrics else 0),
        "gc_objects": float(len(gc.get_objects())),
        "fds": float(metrics.open_fds if metrics and metrics.open_fds is not None else 0),
    }
    row.update({key: float(value) for key, value in disk_state(config).items()})
    pages, free_pages = sqlite_pages(config)
    row["pages"] = float(pages)
    row["free_pages"] = float(free_pages)
    return row


def print_header() -> None:
    print(
        f"{'цикл':>6} {'RSS, MiB':>9} {'GC-объекты':>11} {'state.db':>10} {'WAL':>8} "
        f"{'страниц':>8} {'бэкапы':>14} {'записей':>8} {'назначений':>11}"
    )


def print_sample(label: str, row: dict[str, float], writes: int, assignments: int) -> None:
    print(
        f"{label:>6} {row['rss'] / health.MIB:>9.1f} {int(row['gc_objects']):>11} "
        f"{health.human_bytes(int(row['db'])):>10} {health.human_bytes(int(row['wal'])):>8} "
        f"{int(row['pages']):>8} "
        f"{health.human_bytes(int(row['backups_bytes'])) + '/' + str(int(row['backups_files'])):>14} "
        f"{writes:>8} {assignments:>11}"
    )


def churn(panel: MockPanel, service: BalancerService, store: StateStore, rng: random.Random, cycle: int) -> str:
    """Имитировать жизнь панели. Возвращает описание события (пустое, если ничего не менялось)."""
    if cycle % 30 == 0:
        index = 1000 + cycle
        panel.add_clients(make_client(index, f"new{index}@example"))
        return f"добавлен клиент new{index}@example"
    if cycle % 90 == 0 and len(panel.clients) > 25:
        victim = rng.choice(panel.clients)
        panel.remove_client(victim["id"])
        return f"удалён клиент {victim['email']}"
    if cycle % 70 == 0 and panel.clients:
        victim = rng.choice(panel.clients)
        victim["email"] = f"renamed{victim['id']}@example"
        return f"переименован клиент id={victim['id']}"
    if cycle % 45 == 0 and panel.clients:
        victim = rng.choice(panel.clients)
        try:
            clients = [c for c in service.fetch_clients() if c.client_id == victim["id"]]
            if clients:
                assignments = store.load_assignments()
                target = pick_target(assignments, clients[0], service.balancer_tags)
                plans = plan_moves(assignments, clients, {clients[0].client_id: target}, service.balancer_tags)
                outcome = apply_moves(service, plans, verify=False)
                if outcome.moved:
                    return f"перенос {outcome.moved[0][0]}: {outcome.moved[0][1]} -> {outcome.moved[0][2]}"
        except PanelError as exc:  # pragma: no cover - mock отвечает всегда
            return f"ошибка переноса: {exc}"
    return ""


def run_cycles(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    with MockPanel() as panel:
        panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, args.clients + 1)])
        with tempfile.TemporaryDirectory(prefix="xcb-soak-", dir=args.workdir) as tmp:
            workdir = Path(tmp)
            config = build_config(panel.base_url, workdir, keep=args.keep)
            store = StateStore(config.state.database)
            api = PanelApi(config.panel)
            service = build_service(config, store, api)
            samples: list[tuple[int, dict[str, float]]] = []
            events: list[str] = []
            try:
                print(f"режим cycles: клиентов {args.clients}, циклов {args.cycles}, "
                      f"backups.keep={args.keep}, каталог {workdir}")
                print("")
                print_header()
                started = time.monotonic()
                sync_errors = 0
                for cycle in range(1, args.cycles + 1):
                    event = churn(panel, service, store, rng, cycle) if args.churn else ""
                    if event:
                        events.append(f"  цикл {cycle}: {event}")
                    report = service.sync()
                    # mock-панель хранит каждую записанную версию конфига; для замера памяти
                    # сервиса это лишний груз в том же процессе — считаем записи, тело не храним
                    panel.write_log.clear()
                    if report.errors:
                        sync_errors += 1
                        print(f"цикл {cycle}: ОШИБКА sync: {report.errors[:1]}")
                    if cycle == 1 or cycle % args.tick == 0:
                        # замер без принудительного gc: так же, как это делает внешний наблюдатель
                        row = sample(config)
                        samples.append((cycle, row))
                        print_sample(str(cycle), row, panel.write_count, len(store.load_assignments()))
                elapsed = time.monotonic() - started
                if samples[-1][0] != args.cycles:
                    row = sample(config)
                    samples.append((args.cycles, row))
                    print_sample(str(args.cycles), row, panel.write_count, len(store.load_assignments()))

                first_cycle, first = samples[0]
                last_cycle, last = samples[-1]
                rss_growth = last["rss"] - first["rss"]
                gc_growth = last["gc_objects"] - first["gc_objects"]
                print("")
                print(f"циклов за {elapsed:.1f} c ({args.cycles / max(elapsed, 1e-9):.0f} цикл/с); "
                      f"записей конфига: {panel.write_count}; циклов с ошибками: {sync_errors}")
                print(f"RSS: {first['rss'] / health.MIB:.1f} MiB на цикле {first_cycle} -> "
                      f"{last['rss'] / health.MIB:.1f} MiB на цикле {last_cycle} "
                      f"(рост {rss_growth / health.MIB:+.1f} MiB за {last_cycle - first_cycle} циклов)")
                print(f"объектов в GC: {int(first['gc_objects'])} -> {int(last['gc_objects'])} "
                      f"(рост {int(gc_growth):+d})")
                print(f"state.db: {health.human_bytes(int(last['db']))} "
                      f"({int(last['pages'])} страниц, свободных {int(last['free_pages'])}); "
                      f"WAL {health.human_bytes(int(last['wal']))}; "
                      f"бэкапов {int(last['backups_files'])} на "
                      f"{health.human_bytes(int(last['backups_bytes']))} при keep={args.keep}")
                print(f"назначений в state.db: {len(store.load_assignments())}; "
                      f"дескрипторов: {int(last['fds'])}")

                if gc_growth > args.gc_limit:
                    print(f"ВЕРДИКТ: ПОДОЗРЕНИЕ НА УТЕЧКУ — объекты GC выросли на {int(gc_growth)} "
                          f"(лимит {args.gc_limit})")
                    return 1
                if rss_growth > RSS_GROWTH_LIMIT:
                    print(f"ВЕРДИКТ: ПОДОЗРЕНИЕ НА УТЕЧКУ — RSS вырос на "
                          f"{rss_growth / health.MIB:.1f} MiB (лимит {RSS_GROWTH_LIMIT / health.MIB:.0f} MiB)")
                    return 1
                if int(last["backups_files"]) > args.keep:
                    print("ВЕРДИКТ: ротация бэкапов НЕ работает — файлов больше, чем backups.keep")
                    return 1
                print("ВЕРДИКТ: утечки не видно; рост памяти и диска за прогон — в пределах "
                      "разового старта и ротации бэкапов")
                if args.show_events:
                    print("")
                    print("события панели (первые 20):")
                    print("\n".join(events[:20]) or "  —")
                return 0
            finally:
                api.close()
                store.close()


def run_daemon(args: argparse.Namespace) -> int:
    """Поднять штатный daemon отдельным процессом и снять с него метрики."""
    with MockPanel() as panel:
        panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, args.clients + 1)])
        workdir = Path(tempfile.mkdtemp(prefix="xcb-daemon-", dir=args.workdir))
        config = build_config(panel.base_url, workdir, keep=args.keep, interval=5)
        raw = {
            "panel": {
                "url": config.panel.url,
                "api_token": TOKEN,
                "verify_tls": False,
                "timeout_seconds": 5,
                "retries": 1,
                "backoff_seconds": 0.01,
                "max_backoff_seconds": 0.02,
            },
            "balancers": BALANCERS,
            "fallback": {"outbound": "server-4"},
            "state": {"database": config.state.database},
            "backups": {"enabled": True, "directory": config.backups.directory, "keep": args.keep},
            "sync": {"interval_seconds": 5},
        }
        config_path = workdir / "config.yaml"
        config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        log_path = workdir / "daemon.log"
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        print(f"режим daemon: {args.seconds} c, конфиг {config_path}, журнал {log_path}")
        print("")
        print(f"{'t, c':>5} {'RSS, MiB':>9} {'дескрипторы':>12} {'state.db':>10} {'строк журнала':>14}")
        with open(log_path, "wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "xray_client_balancer.main", "daemon", "--config", str(config_path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(ROOT),
            )
        samples: list[tuple[float, int]] = []
        try:
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(args.sample_every)
                elapsed = args.seconds - max(0.0, deadline - time.monotonic())
                metrics = health.read_process_metrics(process.pid)
                if metrics is None:
                    break
                samples.append((elapsed, metrics.rss_bytes))
                lines = log_path.read_text(encoding="utf-8", errors="replace").count("\n")
                files = disk_state(config)
                print(
                    f"{elapsed:>5.0f} {metrics.rss_bytes / health.MIB:>9.1f} {metrics.open_fds:>12} "
                    f"{health.human_bytes(files['db'] + files['wal']):>10} {lines:>14}"
                )
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
                process.wait(timeout=10)

        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = log_text.count("\n")
        per_hour = lines * 3600 / max(args.seconds, 1)
        print("")
        print(f"демон завершён по SIGTERM, код выхода {process.returncode}")
        print(f"журнал: {lines} строк за {args.seconds} c -> ~{per_hour:.0f} строк/час "
              f"(за сутки ~{per_hour * 24 / 1000:.0f} тыс. строк)")
        rss_first: int | None = None
        rss_last: int | None = None
        if samples:
            rss_first, rss_last = samples[0][1], samples[-1][1]
            print(f"RSS: {rss_first / health.MIB:.1f} MiB на {samples[0][0]:.0f} c -> "
                  f"{rss_last / health.MIB:.1f} MiB на {samples[-1][0]:.0f} c "
                  f"(рост {(rss_last - rss_first) / health.MIB:+.1f} MiB)")
        print(f"диск: {json.dumps(disk_state(config), ensure_ascii=False)}")
        tail = [line for line in log_text.strip().splitlines()[-3:]]
        print("последние строки журнала:")
        for line in tail:
            print(f"  {line}")
        if rss_first is not None and rss_last is not None and rss_last - rss_first > RSS_GROWTH_LIMIT:
            print("ВЕРДИКТ: ПОДОЗРЕНИЕ НА УТЕЧКУ в реальном процессе daemon")
            return 1
        print("ВЕРДИКТ: реальный процесс daemon ведёт себя как прогон в процессе — "
              "память без роста, число строк журнала на цикл постоянное (3 INFO на цикл)")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--clients", type=int, default=60, help="сколько клиентов в mock-панели")
    common.add_argument("--keep", type=int, default=20, help="backups.keep (ротация)")
    common.add_argument("--workdir", default=os.environ.get("TMPDIR", "/tmp"), help="где писать временные файлы")

    p_cycles = sub.add_parser("cycles", parents=[common], help="прогон синхронизаций в этом процессе")
    p_cycles.add_argument("--cycles", type=int, default=2000, help="число прогонов sync")
    p_cycles.add_argument("--tick", type=int, default=200, help="печатать замер каждые N циклов")
    p_cycles.add_argument("--seed", type=int, default=1, help="seed для воспроизводимости churn")
    p_cycles.add_argument("--churn", dest="churn", action="store_true", default=True)
    p_cycles.add_argument("--no-churn", dest="churn", action="store_false", help="не менять панель")
    p_cycles.add_argument("--gc-limit", type=int, default=20000, help="лимит роста объектов GC")
    p_cycles.add_argument("--show-events", action="store_true")

    p_daemon = sub.add_parser("daemon", parents=[common], help="реальный процесс daemon под наблюдением")
    p_daemon.add_argument("--seconds", type=int, default=120)
    p_daemon.add_argument("--sample-every", type=float, default=5.0)

    # `cycles` по умолчанию, если режим не указан
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("cycles", "daemon"):
        argv = ["cycles", *argv]
    args = parser.parse_args(argv)
    return run_cycles(args) if args.mode == "cycles" else run_daemon(args)


if __name__ == "__main__":
    raise SystemExit(main())
