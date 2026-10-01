"""CLI сервиса: daemon, sync, status, list, rebalance, check-api."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import __version__, health, routing
from .allocator import rebalance_assignments, rebalance_diff
from .api import PanelApi, PanelError
from .config import AppConfig, ConfigError, DEFAULT_CONFIG_PATH, load_config
from .database import StateStore
from .models import Assignment, PanelClient, group_assignments
from .ops import (
    EXIT_REFUSED,
    MoveError,
    MovePlan,
    apply_moves,
    check_eligibility,
    find_balancer_tag,
    pick_target_counted,
    plan_moves,
    resolve_client,
)
from .service import (
    META_CONFIG_WRITES,
    META_LAST_CONFIG_UPDATE,
    META_LAST_ERROR,
    META_LAST_SYNC,
    BalancerService,
    BackupStore,
    distribution_of,
)

logger = logging.getLogger("xray_client_balancer")

# Раз в час демон пишет в журнал замер ресурсов: по этим строкам видно, растёт ли
# память и файлы со временем, без запуска отдельных инструментов (и без cron).
RESOURCE_LOG_INTERVAL = 3600.0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # httpx/httpcore логируют полный URL запроса, а в URL панели лежит секретный base path:
    # поднимаем им порог, чтобы base path и токен не попадали в journal
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "never"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return value


def build_service(config: AppConfig) -> tuple[BalancerService, StateStore, PanelApi]:
    store = StateStore(config.state.database)
    api = PanelApi(config.panel)
    service = BalancerService(
        config,
        store,
        api,
        backups=BackupStore(config.backups.directory, config.backups.keep, config.backups.enabled),
    )
    return service, store, api


# --------------------------------------------------------------------------- commands


def cmd_sync(config: AppConfig, args: argparse.Namespace) -> int:
    service, store, api = build_service(config)
    try:
        report = service.sync(dry_run=args.dry_run, force_write=args.force_write)
        print(report.render(config))
        return 1 if report.errors else 0
    finally:
        store.close()
        api.close()


def cmd_rebalance(config: AppConfig, args: argparse.Namespace) -> int:
    """Ручное выравнивание (§21). Никогда не запускается автоматически."""
    service, store, api = build_service(config)
    try:
        assignments = store.load_assignments()
        try:
            clients = [c for c in service.fetch_clients() if config.include_disabled or c.active]
        except PanelError as exc:
            print(f"ERROR: не удалось получить список клиентов: {exc}", file=sys.stderr)
            return 1
        clients = [c for c in clients if not config.is_excluded(c.email)]
        if not clients:
            print("Клиентов нет — нечего выравнивать.")
            return 0
        target = rebalance_assignments(clients, service.specs)
        changed = rebalance_diff(assignments, target)
        by_id = {c.client_id: c.email for c in clients}

        current = distribution_of(assignments, service.balancer_tags)
        after: dict[str, int] = {tag: 0 for tag in service.balancer_tags}
        for tag in target.values():
            after[tag] += 1

        print("Current:")
        print("")
        for tag in service.balancer_tags:
            print(f"  {tag.split('client-balancer-')[-1].upper()}: {current[tag]}")
        print("")
        print("After rebalance:")
        print("")
        for tag in service.balancer_tags:
            print(f"  {tag.split('client-balancer-')[-1].upper()}: {after[tag]}")
        print("")
        print(f"{len(changed)} client assignments will change.")
        if changed:
            print("")
            for client_id in sorted(changed, key=lambda cid: by_id.get(cid, "")):
                old = assignments.get(client_id)
                print(f"  {by_id.get(client_id, client_id)}: {old.balancer_tag if old else '-'} -> {changed[client_id]}")

        if args.dry_run or not args.yes:
            print("")
            print("Ничего не изменено. Для применения повторите с --yes.")
            return 0
        if not changed:
            print("")
            print("Изменений нет — записывать нечего.")
            return 0

        template = api.get_xray_template()
        groups = group_assignments(
            [
                Assignment(
                    client_id=client_id,
                    email=by_id[client_id],
                    balancer_tag=tag,
                    created_at=assignments[client_id].created_at if client_id in assignments else 0,
                    updated_at=assignments[client_id].updated_at if client_id in assignments else 0,
                )
                for client_id, tag in target.items()
            ],
            service.balancer_tags,
        )
        candidate = service.build_candidate(template, groups)
        problems = routing.validate_candidate(candidate, groups, service.specs)
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}", file=sys.stderr)
            print("Конфиг не применён: кандидат невалиден.", file=sys.stderr)
            return 1

        service.backups.save(template, note="before manual rebalance")
        store.replace_all({cid: (by_id[cid], tag) for cid, tag in target.items()})
        try:
            api.update_xray_template(candidate)
        except PanelError as exc:
            print(f"ERROR: панель не приняла конфиг: {exc}", file=sys.stderr)
            print("Локальная БД обновлена — следующий sync догонит конфиг.", file=sys.stderr)
            return 1
        print("")
        print("Rebalance применён: локальная БД и routing обновлены.")
        return 0
    finally:
        store.close()
        api.close()


def cmd_status(config: AppConfig, args: argparse.Namespace) -> int:
    store = StateStore(config.state.database)
    api = PanelApi(config.panel)
    try:
        assignments = store.load_assignments()
        counts = distribution_of(assignments, config.balancer_tags)
        meta = store.meta()
        print(f"Clients: {len(assignments)}")
        print("")
        for tag in config.balancer_tags:
            print(f"{tag}: {counts[tag]}")
        print("")
        print("Fallback:")
        print(config.fallback.outbound)
        print("")
        print("Last successful sync:")
        print(_fmt_ts(meta.get(META_LAST_SYNC)))
        print("")
        print("Last config update:")
        print(_fmt_ts(meta.get(META_LAST_CONFIG_UPDATE)))
        print("")
        print(f"Config writes: {meta.get(META_CONFIG_WRITES, '0')}")
        print(f"Last error: {meta.get(META_LAST_ERROR) or 'none'}")
        print(f"State DB: {config.state.database} ({store.database_size_bytes} bytes)")

        try:
            template = api.get_xray_template()
        except PanelError as exc:
            print("")
            print(f"Panel: НЕДОСТУПНА ({exc})")
            return 1

        block = routing.describe_managed_block(template, config.balancer_tags)
        print("")
        print("Managed routing rules:")
        if not block:
            print("  нет — сервис ещё не записывал правила")
        for item in block:
            print(f"  index={item['index']} balancer={item['balancerTag']} users={item['users']}")

        ours = set(config.balancer_tags)
        print("")
        print("Balancers in Xray config:")
        for bal in (template.get("routing") or {}).get("balancers") or []:
            if not isinstance(bal, dict):
                continue
            marker = " (managed)" if str(bal.get("tag")) in ours else " (foreign)"
            strategy = (bal.get("strategy") or {}).get("type") if isinstance(bal.get("strategy"), dict) else bal.get("strategy")
            print(
                f"  {bal.get('tag')}{marker}: selector={bal.get('selector')} "
                f"strategy={strategy} fallbackTag={bal.get('fallbackTag')}"
            )
        for section in ("observatory", "burstObservatory"):
            if section in template:
                tags = (template.get(section) or {}).get("subjectSelector")
                print(f"  {section}: subjectSelector={tags}")

        status = api.balancer_status(config.balancer_tags)
        if status:
            print("")
            print("Live balancers (from running core):")
            for tag, info in status.items():
                if isinstance(info, dict):
                    print(
                        f"  {tag}: running={info.get('running')} selected={info.get('selected')} "
                        f"override={info.get('override') or '-'}"
                    )
        return 0
    finally:
        store.close()
        api.close()


def cmd_list(config: AppConfig, args: argparse.Namespace) -> int:
    store = StateStore(config.state.database)
    try:
        assignments = store.load_assignments()
        groups = group_assignments(assignments.values(), config.balancer_tags)
        print(f"Clients: {len(assignments)}")
        for tag in config.balancer_tags:
            emails = groups.get(tag, [])
            print("")
            print(f"{tag} ({len(emails)})")
            for email in emails:
                print(f"  {email}")
        return 0
    finally:
        store.close()


def _print_distribution(config: AppConfig, counts: dict[str, int], title: str) -> None:
    """Раскладка с псевдографикой: сразу видно, куда перекос, без чтения чисел."""
    print(title)
    for tag in config.balancer_tags:
        value = counts.get(tag, 0)
        print(f"  {tag:<20} {value:>4}  {'#' * min(value, 40)}")


def _build_move_plans(
    config: AppConfig,
    store: StateStore,
    service: BalancerService,
    panel_clients: Sequence[PanelClient],
    *,
    queries: Sequence[str],
    client_id: int | None,
    tag_text: str | None,
    auto: bool,
) -> tuple[list[MovePlan], list[str]]:
    """Разобрать аргументы в план переноса. Все отказы — через MoveError (exit 2)."""
    tags = service.balancer_tags
    if client_id is not None:
        clients = [resolve_client(panel_clients, client_id=client_id)]
    else:
        clients = [resolve_client(panel_clients, query) for query in queries]
    # один и тот же клиент, названный дважды (email + подстрока), не должен попасть в план дважды
    unique: dict[int, PanelClient] = {client.client_id: client for client in clients}
    clients = list(unique.values())

    fixed_tag: str | None = None
    if tag_text is not None:
        fixed_tag = find_balancer_tag(tags, tag_text)
    elif not auto:  # pragma: no cover - гарантировано разбором аргументов
        raise MoveError("не указан целевой балансировщик")

    assignments = store.load_assignments()
    counts = distribution_of(assignments, tags)
    warnings: list[str] = []
    targets: dict[int, str] = {}
    for client in clients:
        warnings.extend(check_eligibility(config, client))
        if fixed_tag:
            target = fixed_tag
        else:
            # --auto: считаем по раскладке с учётом уже принятых в этом же вызове решений,
            # иначе несколько клиентов уедут в одну и ту же «пустую» группу
            current = assignments.get(client.client_id)
            current_tag = current.balancer_tag if current else None
            target = pick_target_counted(counts, current_tag, tags)
            if current_tag in counts:
                counts[current_tag] -= 1
            counts[target] = counts.get(target, 0) + 1
        targets[client.client_id] = target
    return plan_moves(assignments, clients, targets, tags), warnings


def cmd_move(config: AppConfig, args: argparse.Namespace) -> int:
    """Перевести клиента(ов) в другую группу, не трогая остальных клиентов (§3).

    Правка точечная: меняются назначения только указанных клиентов плюс одно-два
    правила routing, поэтому панель применяет её hot-apply, без перезапуска ядра.
    """
    queries = list(args.clients or [])
    if args.id is not None and queries:
        print("ERROR: укажите либо email позиционно, либо --id N (не оба сразу)", file=sys.stderr)
        return EXIT_REFUSED

    tag_text = args.to_tag
    if tag_text is None and not args.auto:
        if args.id is not None:
            print("ERROR: с --id тег задаётся ключом --to <тег>", file=sys.stderr)
            return EXIT_REFUSED
        if len(queries) != 2:
            print(
                "ERROR: нужно указать клиента и тег:  xcb move <клиент> <тег>\n"
                "       несколько клиентов сразу:     xcb move --to <тег> <клиент> [<клиент> ...]",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        queries, tag_text = queries[:1], queries[1]
    if args.auto and tag_text is not None:
        print("ERROR: --auto и --to вместе не имеют смысла", file=sys.stderr)
        return EXIT_REFUSED

    service, store, api = build_service(config)
    try:
        try:
            panel_clients = service.fetch_clients()
        except PanelError as exc:
            print(f"ERROR: не удалось получить список клиентов панели: {exc}", file=sys.stderr)
            print(
                "Операция отменена: без списка панели нельзя убедиться, что клиент существует.",
                file=sys.stderr,
            )
            return 1

        try:
            plans, warnings = _build_move_plans(
                config,
                store,
                service,
                panel_clients,
                queries=queries,
                client_id=args.id,
                tag_text=tag_text,
                auto=args.auto,
            )
        except MoveError as exc:
            print(f"ОТКАЗ: {exc}", file=sys.stderr)
            if exc.hint:
                print(f"       {exc.hint}", file=sys.stderr)
            return exc.exit_code

        print(f"Клиентов в панели: {len(panel_clients)}; групп: {len(service.balancer_tags)}")
        print("")
        for plan in plans:
            print(f"  {plan.describe()}")
        for warning in warnings:
            print(f"  WARNING: {warning}")
        print("")
        _print_distribution(config, plans[0].counts_before, "Раскладка сейчас:")
        print("")
        _print_distribution(config, plans[-1].counts_after, "Раскладка после переноса:")
        print("")

        if all(plan.is_noop for plan in plans):
            print("Все указанные клиенты уже в этих группах — менять нечего.")
            return 0

        if not args.yes:
            print("Это только план (--yes не указан). Ничего не изменено.")
            print("Применить: повторите команду с --yes")
            return 0

        outcome = apply_moves(service, plans, verify=not args.no_verify)
        print(outcome.render())

        reverse = [(email, was) for email, was, _now in outcome.moved if was != "—"]
        if reverse:
            print("")
            print("Вернуть обратно:")
            for email, was in reverse:
                print(f"  xcb move {email} {was} --yes")
        return 1 if outcome.errors else 0
    finally:
        store.close()
        api.close()


def cmd_clients(config: AppConfig, args: argparse.Namespace) -> int:
    """Список клиентов с id и текущей группой — то, из чего собирается `move`."""
    store = StateStore(config.state.database)
    api: PanelApi | None = None
    try:
        assignments = store.load_assignments()
        panel_clients: list[PanelClient] = []
        panel_error = ""
        if not args.offline:
            try:
                api = PanelApi(config.panel)
                panel_clients = api.get_clients()
            except (PanelError, ConfigError) as exc:
                panel_error = str(exc)

        by_id = {c.client_id: c for c in panel_clients}
        rows: list[tuple[str, int, str, str]] = []
        for client_id in sorted(set(assignments) | set(by_id)):
            assignment = assignments.get(client_id)
            client = by_id.get(client_id)
            email = (client.email if client else None) or (
                assignment.email if assignment else str(client_id)
            )
            tag = assignment.balancer_tag if assignment else "(не назначен)"
            if client is None:
                state = "нет в панели"
            elif not client.enable:
                state = "disabled"
            elif not client.active:
                state = "expired/лимит"
            else:
                state = "active"
            rows.append((tag, client_id, email, state))

        if args.filter:
            needle = args.filter.lower()
            rows = [row for row in rows if needle in row[2].lower()]
        if args.group:
            wanted = find_balancer_tag(config.balancer_tags, args.group)
            rows = [row for row in rows if row[0] == wanted]

        if panel_error:
            print(f"ВНИМАНИЕ: список клиентов панели недоступен ({panel_error}) — показано")
            print("          только локальное состояние; назначения не назначенных клиентов не видны.")
            print("")
        if not args.offline:
            print(f"Клиентов в панели: {len(panel_clients)}; назначений в state.db: {len(assignments)}")
        else:
            print(f"Назначений в state.db: {len(assignments)} (--offline: панель не опрашивалась)")
        print(f"Показано строк: {len(rows)}")
        print("")

        order = [*config.balancer_tags, "(не назначен)"]
        for tag in order:
            bucket = [row for row in rows if row[0] == tag]
            if not bucket:
                continue
            print(f"{tag} ({len(bucket)})")
            for _tag, client_id, email, state in sorted(bucket, key=lambda r: (r[2].lower(), r[1])):
                print(f"  {client_id:>6}  {email:<34} {state}")
            print("")
        foreign = [row for row in rows if row[0] not in order]
        if foreign:
            print(f"прочие группы ({len(foreign)})")
            for tag, client_id, email, state in sorted(foreign):
                print(f"  {client_id:>6}  {email:<34} {state}  [{tag}]")
        return 0
    finally:
        store.close()
        if api is not None:
            api.close()


def cmd_balancers(config: AppConfig, args: argparse.Namespace) -> int:
    """Группы сервиса: чей primary, сколько клиентов, живо ли ядро (--live)."""
    store = StateStore(config.state.database)
    api: PanelApi | None = None
    try:
        assignments = store.load_assignments()
        counts = distribution_of(assignments, config.balancer_tags)
        print(f"Групп в конфиге: {len(config.balancers)}; клиентов в state.db: {len(assignments)}")
        print("")
        for balancer in config.balancers:
            print(f"  {balancer.tag}  (подставляется как сокращение: {balancer.tag.rsplit('-', 1)[-1]})")
            print(f"    primary    {balancer.primary}")
            print(f"    strategy   {balancer.strategy or 'leastPing'}")
            print(f"    fallback   {config.fallback.outbound}")
            print(f"    клиентов   {counts[balancer.tag]}")
        print("")
        if not args.live:
            print("Live-состояние ядра не запрашивалось (для этого: --live)")
            return 0
        api = PanelApi(config.panel)
        status = api.balancer_status(config.balancer_tags)
        print("Live (из работающего ядра):")
        for tag in config.balancer_tags:
            info = status.get(tag)
            if not isinstance(info, dict):
                print(f"  {tag}: нет данных")
                continue
            print(
                f"  {tag}: running={info.get('running')} selected={info.get('selected')} "
                f"override={info.get('override') or '-'}"
            )
        return 0
    except PanelError as exc:
        print(f"ERROR: панель недоступна: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
        if api is not None:
            api.close()


def cmd_doctor(config: AppConfig, args: argparse.Namespace) -> int:
    """Замеры памяти процесса и роста файлов на диске. Ничего не меняет.

    Код выхода 1, если есть предупреждения — удобно для cron/мониторинга.
    """
    findings = health.collect_findings(
        config, pid=args.pid, rss_warn=int(args.rss_warn_mib * health.MIB)
    )
    print("Память и диск: замеры состояния сервиса (ничего не изменяется)")
    print(f"конфиг: {config.state.database} | бэкапы: {config.backups.directory}")
    print("")
    print(health.render_findings(findings))
    if args.pid is None:
        print("")
        print("Подсказка: чтобы измерить сам демон, передайте его pid:")
        print("  xcb doctor --pid $(systemctl show -p MainPID --value xray-client-balancer)")
    return 1 if any(finding.ok is False for finding in findings) else 0


def cmd_daemon(config: AppConfig, args: argparse.Namespace) -> int:
    service, store, api = build_service(config)
    stopping = {"flag": False}

    def _stop(signum: int, _frame: object) -> None:
        logger.info("Получен сигнал %d — завершаю работу", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info(
        "xray-client-balancer %s запущен: панель %s, интервал %ds, БД %s",
        __version__,
        config.panel.url,
        config.sync.interval_seconds,
        config.state.database,
    )
    logger.info("Ресурсы на старте: %s", health.resource_summary(config))
    backoff = 0.0
    next_resource_log = time.monotonic() + RESOURCE_LOG_INTERVAL
    try:
        while not stopping["flag"]:
            started = time.monotonic()
            if started >= next_resource_log:
                logger.info("Ресурсы: %s", health.resource_summary(config))
                next_resource_log = started + RESOURCE_LOG_INTERVAL
            try:
                report = service.sync()
                if report.errors:
                    logger.error("Цикл завершён с ошибками: %s", "; ".join(report.errors))
                    backoff = min(
                        backoff * 2 or config.panel.backoff_seconds, config.panel.max_backoff_seconds
                    )
                else:
                    backoff = 0.0
            except Exception:  # noqa: BLE001 - демон не должен падать
                logger.exception("Непредвиденная ошибка цикла; состояние сохранено")
                backoff = min(
                    backoff * 2 or config.panel.backoff_seconds, config.panel.max_backoff_seconds
                )

            sleep_for = backoff if backoff else float(config.sync.interval_seconds)
            deadline = time.monotonic() + max(0.0, sleep_for - (time.monotonic() - started))
            while not stopping["flag"] and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    finally:
        store.close()
        api.close()
    logger.info("Остановлено")
    return 0


def cmd_check_api(config: AppConfig, args: argparse.Namespace) -> int:
    from .diagnostics import run_checks

    return run_checks(config, verbose=args.verbose)


def _optional_config(args: argparse.Namespace) -> AppConfig | None:
    """Конфиг, если он уже создан: `init` и `outbounds` работают и до появления файла."""
    try:
        return load_config(args.config)
    except ConfigError:
        return None


def _confirm(question: str, *, assume_yes: bool) -> bool:
    """Спросить подтверждение. Без TTY (скрипт, cron) отвечать некому — нужен --yes."""
    from . import setup as setup_mod

    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} — нет терминала: повторите с --yes", file=sys.stderr)
        return False
    answer = setup_mod.ask(f"{question} [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def cmd_outbounds(config: AppConfig | None, args: argparse.Namespace) -> int:
    """Показать исходящие панели — то, из чего собираются балансировщики."""
    from . import setup as setup_mod

    try:
        panel = setup_mod.resolve_panel_config(
            config=config, url=args.panel_url, insecure=args.insecure
        )
    except (ConfigError, setup_mod.SetupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        with setup_mod.open_panel(panel) as api:
            print(f"Панель: {panel.url}")
            items = setup_mod.fetch_outbounds(api, include_internal=args.all)
    except PanelError as exc:
        print(f"ERROR: панель недоступна: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(setup_mod.dump_outbounds(items))
        return 0
    print("")
    print(f"Исходящие ({len(items)}):")
    print(setup_mod.render_outbounds(items))
    if not args.all:
        print("")
        print("Служебные (direct/blackhole/dns/…) скрыты — покажите их ключом --all.")
    return 0


def cmd_init(config: AppConfig | None, args: argparse.Namespace) -> int:
    """Найти исходящие, дать выбрать нужные и собрать конфиг.

    Единственная команда, после которой сервис уже работает: она пишет
    config.yaml и сразу делает первую синхронизацию — балансировщики и правила
    routing создаёт сам сервис, руками в панели ничего делать не нужно.
    """
    from . import setup as setup_mod

    path = Path(args.config or os.environ.get("XCB_CONFIG") or DEFAULT_CONFIG_PATH)
    print("Настройка xray-client-balancer")
    print("")
    if path.exists() and not args.force:
        print(f"Конфиг уже существует: {path}")
        print("Перезаписать его: xcb init --force (прежний файл будет сохранён рядом с .bak)")
        return 1
    if path.exists() and args.force:
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Прежний конфиг сохранён: {backup}")
        print("")
    if path.exists() and config is None:
        print(f"ВНИМАНИЕ: {path} существует, но не читается как конфиг —")
        print("          беру адрес панели из окружения или с узла")
        print("")

    try:
        panel = setup_mod.resolve_panel_config(
            config=config,
            url=args.panel_url,
            insecure=args.insecure,
            print_fn=print,
        )
    except (ConfigError, setup_mod.SetupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Панель: {panel.url}")
    try:
        with setup_mod.open_panel(panel) as api:
            items = setup_mod.fetch_outbounds(api, include_internal=args.all)
    except PanelError as exc:
        print(f"ERROR: панель недоступна: {exc}", file=sys.stderr)
        return 1

    if not items:
        print(
            "ERROR: у панели не нашлось ни одного исходящего. Сначала создайте их в панели "
            "(Исходящие → добавить), затем повторите xcb init.",
            file=sys.stderr,
        )
        return 1

    print("")
    print(f"Исходящие панели ({len(items)}):")
    print(setup_mod.render_outbounds(items))
    if args.list_only:
        return 0

    try:
        if args.primary:
            primaries = setup_mod.parse_selection(args.primary, [i.tag for i in items])
        elif args.yes:
            print("ERROR: с --yes нужно указать балансировщики: --primary tag1,tag2", file=sys.stderr)
            return 1
        else:
            primaries = setup_mod.choose_primaries(items)
        if args.fallback:
            fallback = args.fallback.strip()
            if fallback not in [i.tag for i in items]:
                fallback = setup_mod.parse_selection(fallback, [i.tag for i in items])[0]
        elif args.yes:
            print("ERROR: с --yes нужно указать резерв: --fallback tag", file=sys.stderr)
            return 1
        else:
            fallback = setup_mod.choose_fallback(items, primaries)
        document = setup_mod.build_config_document(
            panel.url,
            primaries,
            fallback,
            strategy=args.strategy,
            verify_tls=panel.verify_tls,
            ca_bundle=panel.ca_bundle,
            tls_verify_hostname=panel.tls_verify_hostname,
            state_database=args.state_db,
            backups_directory=args.backups_dir,
        )
    except setup_mod.SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    new_config = AppConfig.model_validate(document)
    print("")
    print("Будет создано:")
    for balancer in new_config.balancers:
        print(f"  {balancer.tag} -> {balancer.primary}  (резерв: {new_config.fallback.outbound})")
    print("")
    if not _confirm(f"Записать конфиг в {path} и применить это в панели?", assume_yes=args.yes):
        print("Ничего не изменено.")
        return 0

    setup_mod.write_config(path, document)
    print("")
    print(f"Конфиг записан: {path}")

    if args.no_sync:
        print("")
        print("Синхронизация пропущена (--no-sync). Дальше: xcb sync")
        return 0

    # дальнейшая работа идёт по записанному файлу: в нём подстановка ${...},
    # которую сервис разворачивает из окружения — как это сделает демон
    service_config = load_config(path)
    service, store, api = build_service(service_config)
    try:
        report = service.sync(dry_run=args.dry_run)
        print("")
        print(report.render(service_config))
        if report.errors:
            return 1
    finally:
        store.close()
        api.close()

    print("")
    print("Готово. Дальше:")
    print("  systemctl enable --now xray-client-balancer   # постоянная синхронизация")
    print("  xcb status                                    # распределение и живые балансировщики")
    if args.start:
        print("")
        return _start_daemon()
    return 0


def _start_daemon() -> int:
    """Включить и запустить юнит — только когда об этом попросили ключом --start."""
    import subprocess

    result = subprocess.run(
        ["systemctl", "enable", "--now", "xray-client-balancer"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"ERROR: systemctl вернул {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}",
            file=sys.stderr,
        )
        return 1
    print("Служба xray-client-balancer включена и запущена.")
    return 0


def cmd_reset(config: AppConfig, args: argparse.Namespace) -> int:
    """Полная перенастройка с нуля одной командой.

    Свои балансировщики и правила удаляются из конфига, локальное состояние
    (назначения и защитные счётчики) очищается, все клиенты раскладываются заново
    по группам из конфига — и всё это одной записью в панель.
    """
    service, store, api = build_service(config)
    tags = service.balancer_tags
    try:
        try:
            clients = [c for c in service.fetch_clients() if config.include_disabled or c.active]
        except PanelError as exc:
            print(f"ERROR: не удалось получить список клиентов: {exc}", file=sys.stderr)
            return 1
        clients = [c for c in clients if not config.is_excluded(c.email)]
        try:
            template = api.get_xray_template()
        except PanelError as exc:
            print(f"ERROR: не удалось прочитать конфиг панели: {exc}", file=sys.stderr)
            return 1

        if not clients:
            print("Клиентов в панели нет: будут пересобраны только свои правила и балансировщики.")

        by_id = {c.client_id: c.email for c in clients}
        target = rebalance_assignments(clients, service.specs) if clients else {}
        groups = group_assignments(
            [
                Assignment(client_id=cid, email=by_id[cid], balancer_tag=tag, created_at=0, updated_at=0)
                for cid, tag in target.items()
            ],
            tags,
        )
        clean = routing.strip_managed(template, tags, service.observatory_tags)
        candidate = service.build_candidate(clean, groups)
        problems = routing.validate_candidate(
            candidate, groups, service.specs, known_outbound_tags=service.known_outbound_tags()
        )

        print("Полная перенастройка с нуля")
        print("")
        print(f"  групп: {len(tags)} ({', '.join(tags)})")
        print(f"  резерв: {config.fallback.outbound}")
        print(f"  клиентов будет разложено заново: {len(target)}")
        print(f"  свои правила в конфиге: было {len(routing.describe_managed_block(template, tags))}")
        print("")
        counts: dict[str, int] = {tag: 0 for tag in tags}
        for tag in target.values():
            counts[tag] += 1
        _print_distribution(config, counts, "Раскладка после перенастройки:")
        print("")

        if problems:
            for problem in problems:
                print(f"ERROR: {problem}", file=sys.stderr)
            print("Ничего не изменено: сначала приведите конфиг и outbound'ы в порядок.", file=sys.stderr)
            return 1
        if routing.template_changed(template, candidate):
            print("Конфиг в панели будет перезаписан (один раз, с бэкапом).")
        else:
            print("Routing уже соответствует — перезаписываться не будет.")
        print("Локальные назначения будут пересобраны заново.")

        if not args.yes:
            print("")
            print("Это только план. Применить: xcb reset --yes")
            return 0

        backup = service.backups.save(template, note="before reset")
        store.reset_state()
        if target:
            store.replace_all({cid: (by_id[cid], tag) for cid, tag in target.items()})

        if not routing.template_changed(template, candidate):
            print("")
            print("Routing не изменился, запись не потребовалась; назначения пересобраны.")
            return 0

        try:
            api.update_xray_template(candidate)
        except PanelError as exc:
            print(f"ERROR: панель не приняла конфиг: {exc}", file=sys.stderr)
            print(
                "Локальные назначения уже пересобраны — следующий sync допишет конфиг сам.",
                file=sys.stderr,
            )
            return 1

        if not api.wait_until_xray_ready(config.safety.xray_ready_timeout):
            state = {}
            try:
                state = api.xray_state()
            except PanelError:
                pass
            print(
                f"CRITICAL: ядро не поднялось после записи (state={state.get('state')!r}) — "
                "возвращаю прежний конфиг",
                file=sys.stderr,
            )
            problems = service.rollback_to(template, backup)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1

        # запись сделана напрямую, поэтому метки состояния обновляем здесь же:
        # иначе `status` после перенастройки показывал бы «never» и нулевой счётчик
        now = str(int(time.time()))
        store.bump_meta_counter(META_CONFIG_WRITES)
        store.set_meta(META_LAST_CONFIG_UPDATE, now)
        store.set_meta(META_LAST_SYNC, now)
        store.set_meta(META_LAST_ERROR, "")

        print("")
        print("Перенастройка выполнена: конфиг панели и локальные назначения пересобраны.")
        if config.validation.route_test:
            for problem in service.verify_routes(groups):
                print(f"ERROR: {problem}", file=sys.stderr)
            print("Проверка маршрутов: см. журнал (routeTest по одному клиенту из группы).")
        return 0
    finally:
        store.close()
        api.close()


# --------------------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xray-client-balancer",
        description="Sticky-распределение клиентов 3x-ui по балансировщикам Xray",
    )
    parser.add_argument("--config", default=None, help="путь к config.yaml")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--version", action="version", version=f"xray-client-balancer {__version__}")
    # Глобальные ключи дублируем в подкомандах: в systemd-юните удобнее
    # `daemon --config /etc/...`, а argparse без parents=[...] такие ключи отвергает.
    # default=SUPPRESS, чтобы значение из подкоманды перекрывало верхнее только когда задано.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--log-level", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", parents=[common], help="один цикл синхронизации")
    p_sync.add_argument("--dry-run", action="store_true", help="показать план, ничего не менять")
    p_sync.add_argument(
        "--force-write",
        action="store_true",
        help="игнорировать churn-предохранитель и всё равно записать конфиг",
    )
    p_sync.set_defaults(func=cmd_sync)

    p_reb = sub.add_parser("rebalance", parents=[common], help="намеренно выровнять всех клиентов по группам")
    p_reb.add_argument("--yes", action="store_true", help="применить изменения")
    p_reb.add_argument("--dry-run", action="store_true", help="только показать план")
    p_reb.set_defaults(func=cmd_rebalance)

    p_status = sub.add_parser("status", parents=[common], help="состояние сервиса и конфига")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", parents=[common], help="список клиентов по группам")
    p_list.set_defaults(func=cmd_list)

    p_clients = sub.add_parser(
        "clients", parents=[common], help="клиенты с id и группой (аргумент для move)"
    )
    p_clients.add_argument("--filter", default="", help="подстрока email")
    p_clients.add_argument("--group", default="", help="только одна группа (тег или его сокращение)")
    p_clients.add_argument("--offline", action="store_true", help="не опрашивать панель")
    p_clients.set_defaults(func=cmd_clients)

    p_balancers = sub.add_parser(
        "balancers", parents=[common], help="группы сервиса: primary, strategy, число клиентов"
    )
    p_balancers.add_argument("--live", action="store_true", help="спросить состояние у ядра")
    p_balancers.set_defaults(func=cmd_balancers)

    p_move = sub.add_parser(
        "move",
        parents=[common],
        aliases=["assign"],
        help="перевести клиента(ов) в другую группу, не трогая остальных",
        description=(
            "Точечная смена sticky-назначения. По умолчанию только показывает план; "
            "изменения — с --yes. Возврат: та же команда с прежним тегом.\n"
            "Примеры:\n"
            "  xcb move anna@example 2          # показать план\n"
            "  xcb move anna@example 2 --yes    # выполнить (2 = client-balancer-2)\n"
            "  xcb move --to client-balancer-1 anna@example bob@example --yes\n"
            "  xcb move anna@example --auto --yes   # в самую свободную группу\n"
            "  xcb move --id 17 --to 3 --yes    # по внутреннему id клиента"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_move.add_argument("clients", nargs="*", metavar="КЛИЕНТ", help="email клиента (можно подстроку)")
    p_move.add_argument("--to", dest="to_tag", default=None, help="тег целевого балансировщика")
    p_move.add_argument("--id", type=int, default=None, help="внутренний id клиента (вместо email)")
    p_move.add_argument("--auto", action="store_true", help="цель — группа с минимумом клиентов")
    p_move.add_argument("--yes", action="store_true", help="действительно изменить (иначе только план)")
    p_move.add_argument("--no-verify", action="store_true", help="не проверять маршрут через routeTest")
    p_move.set_defaults(func=cmd_move)

    p_doctor = sub.add_parser(
        "doctor", parents=[common], help="память процесса и рост диска: замеры без изменений"
    )
    p_doctor.add_argument("--pid", type=int, default=None, help="pid демона (по умолчанию — свой)")
    p_doctor.add_argument(
        "--rss-warn-mib",
        type=float,
        default=health.RSS_WARN_BYTES / health.MIB,
        help="порог WARNING по RSS процесса, MiB",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_daemon = sub.add_parser("daemon", parents=[common], help="постоянный цикл синхронизации")
    p_daemon.set_defaults(func=cmd_daemon)

    p_check = sub.add_parser("check-api", parents=[common], help="диагностика API панели (аналог tools/test_api.py)")
    p_check.add_argument("--verbose", action="store_true", help="печатать детали ответов (без секретов)")
    p_check.set_defaults(func=cmd_check_api)

    # init/outbounds не требуют существующего конфига: они его как раз и создают
    p_outbounds = sub.add_parser(
        "outbounds",
        parents=[common],
        help="показать исходящие панели (кандидаты в балансировщики)",
    )
    p_outbounds.add_argument("--panel-url", default=None, help="URL панели (по умолчанию — из конфига/env)")
    p_outbounds.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат панели")
    p_outbounds.add_argument("--all", action="store_true", help="показать и служебные (direct/blackhole/dns)")
    p_outbounds.add_argument("--json", dest="as_json", action="store_true", help="машинный вывод (JSON)")
    p_outbounds.set_defaults(func=cmd_outbounds, needs_config=False)

    p_init = sub.add_parser(
        "init",
        parents=[common],
        help="найти исходящие, выбрать нужные и создать конфиг",
        description=(
            "Первичная настройка: сервис читает исходящие панели, даёт выбрать, какие\n"
            "станут балансировщиками и какой — общим резервом, записывает config.yaml и\n"
            "сразу создаёт балансировщики и правила routing в панели.\n"
            "Примеры:\n"
            "  xcb init                       # спросит про каждый шаг\n"
            "  xcb init --list-only           # только показать исходящие\n"
            "  xcb init --primary sub1,sub2 --fallback sub3 --yes   # без вопросов"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_init.add_argument("--panel-url", default=None, help="URL панели (по умолчанию — автоопределение/из env)")
    p_init.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат панели")
    p_init.add_argument("--primary", default=None, help="балансировщики: номера или теги через запятую")
    p_init.add_argument("--fallback", default=None, help="общий резерв: номер или тег")
    p_init.add_argument("--strategy", default="leastLoad", help="стратегия Xray для групп (по умолчанию leastLoad)")
    p_init.add_argument("--all", action="store_true", help="включить в список выбора служебные исходящие")
    p_init.add_argument("--state-db", default=None, help="путь к state.db (по умолчанию /var/lib/xray-client-balancer/state.db)")
    p_init.add_argument("--backups-dir", default=None, help="каталог бэкапов (по умолчанию /var/lib/xray-client-balancer/backups)")
    p_init.add_argument("--list-only", action="store_true", help="только показать исходящие и выйти")
    p_init.add_argument("--force", action="store_true", help="перезаписать существующий конфиг (с сохранением .bak)")
    p_init.add_argument("--yes", action="store_true", help="не задавать вопросов (нужны --primary и --fallback)")
    p_init.add_argument("--no-sync", action="store_true", help="только создать конфиг, в панель не писать")
    p_init.add_argument("--dry-run", action="store_true", help="не менять панель: показать план синхронизации")
    p_init.add_argument("--start", action="store_true", help="после настройки включить и запустить службу")
    p_init.set_defaults(func=cmd_init, needs_config=False)

    p_reset = sub.add_parser(
        "reset",
        parents=[common],
        help="полная перенастройка с нуля одной командой",
        description=(
            "Сносит свои балансировщики и правила в панели, очищает локальные назначения\n"
            "и раскладывает всех клиентов заново по группам из config.yaml. По умолчанию\n"
            "показывает план; применяется с --yes."
        ),
    )
    p_reset.add_argument("--yes", action="store_true", help="применить (иначе только план)")
    p_reset.set_defaults(func=cmd_reset)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    setup_logging(args.log_level)
    config: AppConfig | None
    if getattr(args, "needs_config", True):
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            logger.error("Конфигурация: %s", exc)
            return 2
    else:
        # init/outbounds работают и до появления конфига: его ещё нет
        config = _optional_config(args)
    try:
        return int(args.func(config, args))
    except ConfigError as exc:
        # например панель недоступна как *конфигурация*: нет токена или битый ca_bundle
        logger.error("Конфигурация: %s", exc)
        if str(exc).startswith("конфигурация не найдена"):
            logger.error("Сервис ещё не настроен — выполните: xcb init")
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
