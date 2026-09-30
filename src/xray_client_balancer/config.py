"""Загрузка и валидация конфигурации сервиса (pydantic + YAML + подстановка env)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

ENV_TOKEN_NAME = "XRAY_BALANCER_API_TOKEN"
DEFAULT_CONFIG_PATH = "/etc/xray-client-balancer/config.yaml"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(RuntimeError):
    """Конфигурация невалидна — сервис не должен стартовать."""


def _expand_env(value: Any) -> Any:
    """Рекурсивная подстановка ${VAR} из окружения (отсутствующая переменная -> "")."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class PanelConfig(BaseModel):
    """Настройки доступа к REST API 3x-ui."""

    url: str = Field(description="Базовый URL панели вместе с base path, напр. https://127.0.0.1:7119/AbCd/")
    api_token: str = ""
    verify_tls: bool = True
    ca_bundle: str | None = None
    tls_verify_hostname: bool = True
    timeout_seconds: float = 15.0
    retries: int = Field(default=4, ge=0, le=10)
    backoff_seconds: float = Field(default=5.0, gt=0)
    max_backoff_seconds: float = Field(default=30.0, gt=0)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not v:
            raise ValueError("panel.url не задан")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("panel.url должен начинаться с http:// или https://")
        return v.rstrip("/")

    def resolve_token(self) -> str:
        token = (self.api_token or "").strip() or os.environ.get(ENV_TOKEN_NAME, "").strip()
        if not token:
            raise ConfigError(
                f"не задан API-токен: заполните panel.api_token или переменную окружения {ENV_TOKEN_NAME}"
            )
        return token


class BalancerConfig(BaseModel):
    tag: str
    primary: str
    strategy: Literal["leastPing", "random", "roundRobin", "leastLoad"] | None = None
    # тонкая настройка стратегии (например для leastLoad: baselines/expected/tolerance)
    strategy_settings: dict[str, Any] | None = None


class FallbackConfig(BaseModel):
    outbound: str


class RoutingConfig(BaseModel):
    """Как сервис встраивает свои правила в routing.rules."""

    managed_position: Literal["bottom", "top", "after_rule"] = "bottom"
    insert_after_rule: str | None = None
    write_rule_tag: bool = True

    @model_validator(mode="after")
    def _check_marker(self) -> "RoutingConfig":
        if self.managed_position == "after_rule" and not (self.insert_after_rule or "").strip():
            raise ValueError(
                "routing.managed_position=after_rule требует routing.insert_after_rule"
            )
        return self


class ObservatoryConfigModel(BaseModel):
    """Настройки штатного healthcheck Xray (observatory), нужного для fallbackTag."""

    type: Literal["auto", "observatory", "burst"] = "auto"
    probe_url: str = "https://www.gstatic.com/generate_204"
    probe_interval: str = "30s"
    burst_interval: str = "30s"
    burst_timeout: str = "5s"
    burst_sampling: int = Field(default=2, ge=1, le=10)
    burst_http_method: Literal["HEAD", "GET"] = "HEAD"


class SyncConfig(BaseModel):
    interval_seconds: int = Field(default=30, ge=5, le=86400)
    jitter_seconds: int = Field(default=0, ge=0, le=60)


class StateConfig(BaseModel):
    database: str = "/var/lib/xray-client-balancer/state.db"


class BackupConfig(BaseModel):
    enabled: bool = True
    directory: str = "/var/lib/xray-client-balancer/backups"
    keep: int = Field(default=20, ge=1, le=1000)


class SafetyConfig(BaseModel):
    """Защиты, без которых сервис может навредить."""

    refuse_empty_panel: bool = True
    config_repair_attempts: int = Field(default=1, ge=0, le=3)
    churn_breaker_cycles: int = Field(default=3, ge=1, le=100)
    # сколько ждать подъёма ядра после записи; не дождались — откат на предыдущий конфиг
    xray_ready_timeout: float = Field(default=30.0, gt=0, le=300)


class ValidationConfig(BaseModel):
    local_xray_test: bool = False
    xray_binary: str = "/usr/local/x-ui/bin/xray-linux-amd64"
    route_test: bool = True
    route_test_domain: str = "example.com"


class AppConfig(BaseModel):
    panel: PanelConfig
    balancers: list[BalancerConfig]
    fallback: FallbackConfig
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    observatory: ObservatoryConfigModel = Field(default_factory=ObservatoryConfigModel)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    backups: BackupConfig = Field(default_factory=BackupConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    exclude_clients: list[str] = Field(default_factory=list)
    exclude_regex: list[str] = Field(default_factory=list)
    include_disabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "AppConfig":
        if not self.balancers:
            raise ValueError("нужен хотя бы один балансировщик")
        tags = [b.tag for b in self.balancers]
        if len(set(tags)) != len(tags):
            raise ValueError("теги балансировщиков должны быть уникальны")
        for b in self.balancers:
            if b.primary == self.fallback.outbound:
                raise ValueError(f"балансировщик {b.tag}: primary не может совпадать с fallback")
            if b.primary in tags:
                raise ValueError(
                    f"балансировщик {b.tag}: primary должен быть тегом outbound, а не другого балансировщика"
                )
        if self.fallback.outbound in tags:
            raise ValueError("fallback.outbound не может ссылаться на балансировщик")
        for pattern in self.exclude_regex:
            try:
                re.compile(pattern)
            except re.error as exc:  # noqa: PERF203
                raise ValueError(f"exclude_regex '{pattern}' не компилируется: {exc}") from exc
        if self.panel.max_backoff_seconds < self.panel.backoff_seconds:
            raise ValueError("panel.max_backoff_seconds должен быть >= panel.backoff_seconds")
        return self

    @property
    def balancer_tags(self) -> list[str]:
        return [b.tag for b in self.balancers]

    @property
    def excluded_emails(self) -> set[str]:
        return {e.lower() for e in self.exclude_clients}

    def is_excluded(self, email: str) -> bool:
        low = email.lower()
        if low in self.excluded_emails:
            return True
        return any(re.search(p, email) for p in self.exclude_regex)


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Прочитать YAML-конфиг, подставить переменные окружения и провалидировать."""
    raw_path = Path(path or os.environ.get("XCB_CONFIG") or DEFAULT_CONFIG_PATH)
    if not raw_path.exists():
        raise ConfigError(f"конфигурация не найдена: {raw_path}")
    try:
        raw = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML конфигурации невалиден: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("корнем конфигурации должен быть словарь")
    try:
        return AppConfig.model_validate(_expand_env(raw))
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"конфигурация невалидна: {exc}") from exc
