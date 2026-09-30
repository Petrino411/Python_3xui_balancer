"""Общие фикстуры тестов."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_panel import MockPanel, TOKEN, make_client  # noqa: E402

from xray_client_balancer.api import PanelApi  # noqa: E402
from xray_client_balancer.config import AppConfig  # noqa: E402
from xray_client_balancer.database import StateStore  # noqa: E402
from xray_client_balancer.service import BalancerService, BackupStore  # noqa: E402


def make_config(base_url: str, tmp_path: Path, **overrides) -> AppConfig:
    raw = {
        "panel": {
            "url": base_url,
            "api_token": TOKEN,
            "verify_tls": False,
            "timeout_seconds": 5,
            "retries": 1,
            "backoff_seconds": 0.01,
            "max_backoff_seconds": 0.02,
        },
        "balancers": [
            {"tag": "client-balancer-1", "primary": "server-1"},
            {"tag": "client-balancer-2", "primary": "server-2"},
            {"tag": "client-balancer-3", "primary": "server-3"},
        ],
        "fallback": {"outbound": "server-4"},
        "state": {"database": str(tmp_path / "state.db")},
        "backups": {"enabled": True, "directory": str(tmp_path / "backups"), "keep": 3},
    }
    raw.update(overrides)
    return AppConfig.model_validate(raw)


@pytest.fixture()
def panel():
    with MockPanel() as mock:
        yield mock


@pytest.fixture()
def service(panel, tmp_path):
    config = make_config(panel.base_url, tmp_path)
    store = StateStore(config.state.database)
    api = PanelApi(config.panel)
    svc = BalancerService(
        config,
        store,
        api,
        # в тестах не ждём реальные паузы готовности ядра
        sleep=lambda _seconds: None,
        backups=BackupStore(config.backups.directory, config.backups.keep, config.backups.enabled),
    )
    yield svc
    api.close()
    store.close()


@pytest.fixture()
def clients_factory():
    return make_client
