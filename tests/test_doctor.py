"""Тесты `xcb doctor`: замеры памяти и диска не должны врать или падать.

Здесь проверяется, что doctor (а) не требует панели, (б) показывает реальные размеры
файлов состояния и бэкапов, (в) замечает разросшийся WAL, застрявшую ротацию бэкапов
и мусорные файлы рядом с БД — то есть те ситуации, ради которых он и нужен.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_config

from xray_client_balancer import health
from xray_client_balancer.main import build_parser, cmd_doctor
from xray_client_balancer.database import StateStore


def test_process_metrics_of_own_process_are_real() -> None:
    metrics = health.read_process_metrics()
    assert metrics is not None
    assert metrics.rss_bytes > 0
    assert metrics.threads >= 1
    assert metrics.open_fds is not None and metrics.open_fds > 0
    assert metrics.uptime_seconds is None or metrics.uptime_seconds >= 0


def test_process_metrics_unknown_pid_is_none() -> None:
    assert health.read_process_metrics(999_999_999) is None


def test_collect_findings_reports_real_sizes(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    store = StateStore(config.state.database)
    store.apply_changes({1: "client-balancer-1"}, {1: "user1@example"}, [])
    store.close()
    backup_dir = Path(config.backups.directory)
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "xray-template-20260101-000000.json").write_text("x" * 4096, encoding="utf-8")

    findings = health.collect_findings(config)
    text = health.render_findings(findings)
    names = [f.name for f in findings]
    assert "state.db" in names
    assert any("бэкап" in name for name in names)
    assert any("ФС состояния" == name for name in names)
    assert "ИТОГО" in text
    # в замерах должны быть настоящие числа, а не нули-заглушки
    state_finding = next(f for f in findings if f.name == "state.db")
    assert "KiB" in state_finding.value or "MiB" in state_finding.value
    backup_finding = next(f for f in findings if f.name == "бэкапы шаблона")
    assert "4.0 KiB" in backup_finding.value
    assert not [f for f in findings if f.ok is False], "на healthy-конфиге предупреждений быть не должно"


def test_doctor_flags_stray_files_and_big_wal(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    store = StateStore(config.state.database)
    store.apply_changes({1: "client-balancer-1"}, {1: "user1@example"}, [])
    store.close()
    state_dir = Path(config.state.database).parent
    (state_dir / "xcb-candidate-leftover.json").write_text("{}", encoding="utf-8")
    with open(f"{config.state.database}-wal", "wb") as handle:
        handle.truncate(health.WAL_WARN_BYTES)  # разросшийся WAL (checkpoint не проходит)

    args = build_parser().parse_args(["doctor"])
    assert cmd_doctor(config, args) == 1
    out = capsys.readouterr().out
    assert "WAL не сбрасывается" in out
    assert "xcb-candidate-leftover.json" in out
    assert "предупреждений" in out


def test_doctor_ok_on_fresh_install(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    args = build_parser().parse_args(["doctor"])
    assert cmd_doctor(config, args) == 0
    out = capsys.readouterr().out
    assert "ИТОГО: все проверки памяти и диска в норме" in out
    assert "Подсказка" in out  # без --pid doctor измеряет свой процесс


def test_doctor_ignores_foreign_files_near_state_db(panel, tmp_path, capsys) -> None:
    """Чужие файлы рядом с БД (config.yaml и т.п.) — не повод для WARNING."""
    config = make_config(panel.base_url, tmp_path)
    StateStore(config.state.database).close()
    state_dir = Path(config.state.database).parent
    (state_dir / "config.yaml").write_text("panel: {}\n", encoding="utf-8")
    (state_dir / "notes.txt").write_text("заметки администратора\n", encoding="utf-8")

    args = build_parser().parse_args(["doctor"])
    assert cmd_doctor(config, args) == 0
    out = capsys.readouterr().out
    assert "незакрытые временные файлы: 0 шт." in out
    assert "ИТОГО: все проверки памяти и диска в норме" in out


def test_doctor_with_foreign_pid_reports_rss(panel, tmp_path, capsys) -> None:
    import os

    config = make_config(panel.base_url, tmp_path)
    args = build_parser().parse_args(["doctor", "--pid", str(os.getpid())])
    assert cmd_doctor(config, args) == 0
    out = capsys.readouterr().out
    assert "память процесса pid=" in out
    assert "RSS" in out


def test_doctor_rss_threshold_is_configurable(panel, tmp_path, capsys) -> None:
    config = make_config(panel.base_url, tmp_path)
    args = build_parser().parse_args(["doctor", "--rss-warn-mib", "0.001"])
    assert cmd_doctor(config, args) == 1
    assert "порог WARNING" in capsys.readouterr().out


def test_resource_summary_contains_memory_and_disk(panel, tmp_path) -> None:
    config = make_config(panel.base_url, tmp_path)
    StateStore(config.state.database).close()
    summary = health.resource_summary(config)
    for token in ("rss=", "fds=", "state=", "wal=", "backups=", "fs_free="):
        assert token in summary


def test_dir_usage_counts_recursively(tmp_path) -> None:
    nested = tmp_path / "backups" / "sub"
    nested.mkdir(parents=True)
    (nested / "a.json").write_text("0123456789", encoding="utf-8")
    (tmp_path / "backups" / "b.json").write_text("01234", encoding="utf-8")
    usage = health.path_usage(tmp_path / "backups")
    assert usage.files == 2
    assert usage.bytes_total == 15
    missing = health.path_usage(tmp_path / "нет-такого")
    assert missing.exists is False and missing.files == 0
