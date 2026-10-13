"""Async database layer with explicit degradation semantics.

Why this file matters more than it looks
----------------------------------------
A crisis platform loses its database at exactly the moment the database is most
loaded. CUSTOS therefore treats storage as an *unreliable but recoverable*
dependency:

* the connection pool is bounded and timeouts are short, so a hung database
  cannot exhaust the event loop;
* transient failures are retried with jittered backoff;
* when retries are exhausted the platform flips to :data:`OperatingMode.READ_ONLY`
  and keeps serving the last-known twin state (which the command UI labels as
  stale) instead of returning 500s to an EOC;
* every mode transition is written to the audit log and surfaced in ``/health``.

PostgreSQL + async psycopg is the production target. SQLite via aiosqlite exists
solely so a responder laptop or a single-node EOC fallback can hold a local
twin; it is never a multi-operator store.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, DisconnectionError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings
from ..observability import METRICS, get_logger
from ..domain.enums import OperatingMode

log = get_logger("db")

T = TypeVar("T")

#: errors worth retrying: the connection broke, the server restarted, the pool
#: timed out. Not worth retrying: constraint violations, syntax errors, auth.
_TRANSIENT = (OperationalError, InterfaceError, DisconnectionError)


class StorageUnavailable(RuntimeError):
    """Raised when the platform must answer from cached state only."""


class DatabaseHealth:
    """Tracks reachability and drives the platform's operating mode."""

    def __init__(self) -> None:
        self.available: bool = True
        self.read_only: bool = False
        self.last_ok_at: float | None = None
        self.last_error_at: float | None = None
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.recoveries: int = 0
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> OperatingMode:
        if self.read_only:
            return OperatingMode.READ_ONLY
        if not self.available:
            return OperatingMode.DEGRADED
        return OperatingMode.NORMAL

    async def record_success(self) -> None:
        async with self._lock:
            self.last_ok_at = time.time()
            if not self.available or self.read_only:
                self.recoveries += 1
                log.warning("storage recovered - resuming normal writes", extra={"custos": {"downtime_s": self._downtime()}})
            self.available = True
            self.read_only = False
            self.consecutive_failures = 0

    async def record_failure(self, exc: BaseException, *, fatal: bool = False) -> None:
        async with self._lock:
            self.last_error_at = time.time()
            self.last_error = f"{type(exc).__name__}: {exc}"[:500]
            self.consecutive_failures += 1
            self.available = False
            # after a short grace period the platform stops accepting commands
            # that it cannot durably record - an unlogged order is worse than none
            if fatal or self.consecutive_failures >= 3:
                self.read_only = True

    def _downtime(self) -> float:
        if self.last_error_at is None:
            return 0.0
        return round(time.time() - self.last_error_at, 1)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "available": self.available,
            "read_only": self.read_only,
            "mode": self.mode.value,
            "last_ok_age_s": None if self.last_ok_at is None else round(now - self.last_ok_at, 1),
            "last_error_age_s": None if self.last_error_at is None else round(now - self.last_error_at, 1),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "recoveries": self.recoveries,
        }


HEALTH = DatabaseHealth()

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _connect_args() -> dict[str, Any]:
    if settings.is_postgres:
        return {
            "application_name": "custos-api",
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
            "connect_timeout": 5,
        }
    return {"timeout": settings.sqlite_busy_timeout_ms / 1000.0}


def _engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "echo": False,
        "pool_pre_ping": True,          # cheap liveness check before checkout
        "connect_args": _connect_args(),
    }
    if settings.is_postgres:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=10,
            pool_recycle=1800,
            pool_use_lifo=True,          # keep the working set hot under burst load
        )
    else:
        kwargs.update(poolclass=None, pool_size=1, max_overflow=0, pool_timeout=15)
    return kwargs


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        kwargs = _engine_kwargs()
        kwargs.pop("poolclass", None)
        _engine = create_async_engine(settings.database_url, **kwargs)
        if not settings.is_postgres:
            _install_sqlite_pragmas(_engine)
    return _engine


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """WAL + foreign keys + busy timeout: makes SQLite safe for real concurrency."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - driver level
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        finally:
            cursor.close()


def dialect_name() -> str:
    """``postgresql`` | ``sqlite`` | ``uninitialised`` - never raises."""
    return _engine.dialect.name if _engine is not None else "uninitialised"


def is_initialised() -> bool:
    return _engine is not None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _sessionmaker


@contextlib.asynccontextmanager
async def session_scope(*, commit: bool = True) -> AsyncIterator[AsyncSession]:
    """Transactional session. Rolls back on any exception, records health."""
    session = get_sessionmaker()()
    try:
        yield session
        if commit:
            await session.commit()
    except _TRANSIENT as exc:
        await session.rollback()
        await HEALTH.record_failure(exc)
        raise _RetryableStorageError(str(exc)) from exc
    except SQLAlchemyError as exc:
        await session.rollback()
        await HEALTH.record_failure(exc, fatal=True)
        raise
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
        METRICS.inc("db.session")


class _RetryableStorageError(RuntimeError):
    pass


async def with_retry(
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.15,
    commit: bool = True,
    read_only_ok: bool = True,
) -> T:
    """Run ``operation`` inside a fresh session, retrying transient storage errors.

    ``read_only_ok=False`` is used for writes that must never be silently skipped
    (audit entries, committed orders) - those raise :class:`StorageUnavailable`.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        if HEALTH.read_only and not read_only_ok:
            raise StorageUnavailable(HEALTH.last_error or "storage in read-only mode")
        try:
            async with session_scope(commit=commit) as session:
                result = await operation(session)
            await HEALTH.record_success()
            return result
        except _RetryableStorageError as exc:
            last = exc
            METRICS.inc("db.retry")
            if attempt == attempts:
                break
            # full jitter: avoids a thundering herd of reconnects after a blip
            await asyncio.sleep(random.uniform(0, base_delay * (2 ** (attempt - 1))))
        except SQLAlchemyError as exc:
            last = exc
            break
    assert last is not None
    if isinstance(last, _RetryableStorageError):
        raise StorageUnavailable(str(last)) from last
    raise last


async def ping() -> dict[str, Any]:
    """Cheap readiness probe used by /health and the Windows supervisor."""
    t0 = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        await HEALTH.record_success()
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception as exc:  # noqa: BLE001 - health probes must never raise
        await HEALTH.record_failure(exc)
        return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 2), "error": str(exc)[:300]}


async def init_db(*, create_tables: bool | None = None) -> dict[str, Any]:
    """Create the schema when permitted. Production uses Alembic (`make migrate`)."""
    from .models import Base

    create = settings.auto_migrate if create_tables is None else create_tables
    engine = get_engine()
    if not create:
        return {"created": False, "reason": "auto_migrate disabled"}
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await HEALTH.record_success()
    log.info("schema ensured", extra={"custos": {"tables": len(Base.metadata.tables), "dialect": engine.dialect.name}})
    return {"created": True, "tables": len(Base.metadata.tables), "dialect": engine.dialect.name}


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
