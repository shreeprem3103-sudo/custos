"""Typed runtime configuration.

Design rules
------------
* Every knob is environment-driven so an EOC can be reconfigured without a
  rebuild (air-gapped sites cannot pull images on demand).
* Configuration is validated at import time: a bad value must fail *loudly at
  boot*, never silently at 03:00 during a flood.
* Production requires explicit secrets; development may run with an ephemeral
  one so that a fresh checkout always boots.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]

_WEAK_SECRETS = ("", "changeme", "secret", "dev", "custos")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CUSTOS_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- identity ----------------------------------------------------------
    app_name: str = "CUSTOS Crisis Digital Twin"
    environment: Environment = "development"
    log_level: str = "INFO"

    # -- city profile (MVP scope: one city) --------------------------------
    city_id: str = "kochi"
    city_name: str = "Kochi (Ernakulam)"
    city_timezone: str = "Asia/Kolkata"
    city_bbox: str = "76.18,9.85,76.42,10.12"

    # -- database ----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./custos_dev.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_statement_timeout_ms: int = 15_000
    auto_migrate: bool = True
    sqlite_busy_timeout_ms: int = 10_000

    # -- local AI ----------------------------------------------------------
    ai_enabled: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_timeout_s: float = 45.0
    ollama_max_concurrency: int = 2
    ai_require_grounding: bool = True

    # -- security ----------------------------------------------------------
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 43_200
    api_keys: str = ""
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_rpm: int = 600
    simulator_admin_roles: str = "commander,analyst"

    # -- simulation --------------------------------------------------------
    sim_ensemble_size: int = 48
    sim_max_ensemble_size: int = 400
    sim_horizon_minutes: int = 360
    sim_timestep_minutes: int = 15
    sim_max_concurrency: int = 3

    # -- fusion / connectors ----------------------------------------------
    enable_live_connectors: bool = False
    weather_api_url: str = ""
    sensor_stream_url: str = ""
    utility_api_url: str = ""
    poll_interval_s: int = 30
    seed_on_startup: bool = True

    # -- derived -----------------------------------------------------------
    boot_warnings: list[str] = Field(default_factory=list, exclude=True)

    # ------------------------------------------------------------------ #
    # validators
    # ------------------------------------------------------------------ #
    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level {v!r}")
        return v

    @field_validator("database_url")
    @classmethod
    def _db_url(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            # force the async psycopg driver: a blocking driver inside an async
            # worker silently serialises the whole event loop under load.
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg://", 1)
        return v

    @field_validator("city_bbox")
    @classmethod
    def _bbox(cls, v: str) -> str:
        parts = [p.strip() for p in v.split(",")]
        if len(parts) != 4:
            raise ValueError("CUSTOS_CITY_BBOX must be 'min_lon,min_lat,max_lon,max_lat'")
        try:
            min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError("CUSTOS_CITY_BBOX values must be numeric") from exc
        if not (min_lon < max_lon and min_lat < max_lat):
            raise ValueError("CUSTOS_CITY_BBOX min values must be less than max values")
        return v

    @model_validator(mode="after")
    def _harden(self) -> "Settings":
        warn = self.boot_warnings

        if self.environment == "production":
            if self.jwt_secret.strip().lower() in _WEAK_SECRETS or len(self.jwt_secret) < 32:
                raise ValueError(
                    "CUSTOS_JWT_SECRET must be a >=32 char random value in production "
                    '(python -c "import secrets;print(secrets.token_urlsafe(48))")'
                )
            if self.database_url.startswith("sqlite"):
                warn.append(
                    "production environment is running on SQLite: this is a single-"
                    "operator degradation path, not a multi-operator store"
                )
            if not self.api_keys.strip():
                warn.append("no CUSTOS_API_KEYS configured: machine ingestion is disabled")
        elif not self.jwt_secret:
            object.__setattr__(self, "jwt_secret", secrets.token_urlsafe(48))
            warn.append(
                "CUSTOS_JWT_SECRET was not set: generated an ephemeral development secret "
                "(tokens are invalidated on restart)"
            )

        if self.database_url.startswith("sqlite") and self.db_pool_size > 1:
            object.__setattr__(self, "db_pool_size", 1)
            object.__setattr__(self, "db_max_overflow", 0)

        if self.sim_ensemble_size > self.sim_max_ensemble_size:
            raise ValueError("sim_ensemble_size cannot exceed sim_max_ensemble_size")
        if self.sim_timestep_minutes <= 0 or self.sim_horizon_minutes <= 0:
            raise ValueError("simulation horizon and timestep must be positive")
        return self

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @property
    def bbox_tuple(self) -> tuple[float, float, float, float]:
        return tuple(float(p) for p in self.city_bbox.split(","))  # type: ignore[return-value]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_roles(self) -> set[str]:
        return {r.strip() for r in self.simulator_admin_roles.split(",") if r.strip()}

    @property
    def api_key_map(self) -> dict[str, str]:
        """Parse ``key:role,key:role`` into a mapping. Keys are compared by hash."""
        out: dict[str, str] = {}
        for chunk in self.api_keys.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            key, _, role = chunk.partition(":")
            if key and role:
                out[key] = role.strip()
        return out

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def timesteps(self) -> int:
        return max(1, self.sim_horizon_minutes // self.sim_timestep_minutes)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
