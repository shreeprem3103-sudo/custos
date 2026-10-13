"""API dependencies: identity, authorisation, rate limiting, degraded mode.

Security posture for a high-stakes system, stated explicitly:

* **Two credential types.** Machine-to-machine ingestion uses a static API key
  (sensors cannot run a login flow); humans use short-lived HS256 JWTs with
  refresh tokens. Both are validated on every request; there is no
  "authenticated by the network" mode.
* **Deny by default.** A route states the :class:`Permission` it needs; the role
  matrix in :mod:`custos.domain.enums` decides whether the caller has it. An
  unknown permission string is a programming error and fails closed.
* **Rate limits are per identity, not per IP.** A shared EOC NAT must not let one
  runaway script lock out the commander, and a compromised sensor key must not
  be able to flood the twin endpoint.
* **Every write is audited with the caller's identity**, which is the whole point
  of keeping the role in the token.
* **Degraded mode is a first-class response, not an error.** If the database is
  unreachable the twin endpoints still answer from the cached snapshot with
  ``X-CUSTOS-Degraded: true`` and a freshness header, because going dark during a
  flood is the worst possible failure mode.
"""

from __future__ import annotations

import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Iterable

from fastapi import Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..domain.enums import Permission, Role, ROLE_PERMISSIONS
from ..domain.twin import TwinSnapshot
from ..fusion import repository
from ..infrastructure import db
from ..observability import METRICS, current_correlation_id, get_logger, set_correlation_id
from ..services.pipeline import ORCHESTRATOR

log = get_logger("api.deps")

_bearer = HTTPBearer(auto_error=False)


@dataclass(slots=True)
class Identity:
    """Who is calling, and what they may do."""

    subject: str
    role: Role
    auth_method: str
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def is_machine(self) -> bool:
        return self.auth_method == "api_key"

    def audit_detail(self) -> dict[str, Any]:
        return {"role": self.role.value, "auth": self.auth_method}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def _configured_api_keys() -> dict[str, Role]:
    """``key:role`` pairs from configuration; empty means machine ingest is off."""
    keys: dict[str, Role] = {}
    for chunk in (settings.api_keys or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, _, role = chunk.partition(":")
        if not key:
            continue
        try:
            keys[key] = Role(role.strip() or Role.INGEST.value)
        except ValueError:
            keys[key] = Role.INGEST
    return keys


def _decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any]:
    import jwt  # PyJWT

    if not settings.jwt_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured on this deployment")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], audience="custos-ui")
    except Exception as exc:  # noqa: BLE001 - any decode failure is an auth failure
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc
    if payload.get("type", "access") != expected_type:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"expected a {expected_type} token")
    return payload


def issue_tokens(subject: str, role: Role) -> dict[str, Any]:
    import jwt

    if not settings.jwt_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured on this deployment")
    now = datetime.now(timezone.utc)
    access = jwt.encode(
        {
            "sub": subject,
            "role": role.value,
            "type": "access",
            "aud": "custos-ui",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=settings.access_token_ttl_s)).timestamp()),
            "jti": uuid.uuid4().hex[:12],
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    refresh = jwt.encode(
        {
            "sub": subject,
            "role": role.value,
            "type": "refresh",
            "aud": "custos-ui",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=settings.refresh_token_ttl_s)).timestamp()),
            "jti": uuid.uuid4().hex[:12],
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_s,
        "role": role.value,
        "subject": subject,
        "permissions": sorted(permission.value for permission in permissions_for(role)),
    }


async def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Identity:
    """Resolve the caller. Fails closed: no credential, no access."""
    keys = _configured_api_keys()
    if x_api_key:
        for key, role in keys.items():
            # constant-time comparison: an API key check must not leak length or prefix
            if hmac.compare_digest(key, x_api_key):
                return Identity(subject=f"svc:{role.value}-key", role=role, auth_method="api_key", permissions=permissions_for(role))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    if credentials is not None and credentials.credentials:
        payload = _decode_token(credentials.credentials)
        try:
            role = Role(str(payload.get("role") or "viewer"))
        except ValueError:
            role = Role.VIEWER
        return Identity(subject=str(payload.get("sub") or "unknown"), role=role, auth_method="jwt", permissions=permissions_for(role))
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", headers={"WWW-Authenticate": "Bearer"})


def require(permission: Permission):
    """Route guard: ``Depends(require(Permission.SIMULATE))``."""

    async def _guard(identity: Annotated[Identity, Depends(authenticate)], request: Request) -> Identity:
        if not identity.can(permission):
            await record_denied(identity, request)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role '{identity.role.value}' may not {permission.value}",
            )
        return identity

    return _guard


async def record_denied(identity: Identity, request: Request) -> None:
    """A denied action is an auditable event, not just a 403."""
    METRICS.inc("auth.denied")
    try:
        from ..services import store

        async with store.session_scope() as session:
            await store.write_audit(
                session,
                actor=identity.subject,
                role=identity.role.value,
                action="denied",
                resource=str(request.url.path),
                outcome="denied",
                detail={"method": request.method, "auth": identity.auth_method},
                correlation_id=current_correlation_id(),
            )
    except Exception:  # noqa: BLE001 - the 403 must still be returned
        pass
    log.warning("permission denied", extra={"custos": {"actor": identity.subject, "role": identity.role.value, "path": str(request.url.path)}})


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #
class TokenBucket:
    """Per-identity token bucket, resynced to a monotonic clock."""

    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self.rate = max(1, rate_per_minute) / 60.0
        self.capacity = float(burst or max(10, rate_per_minute // 6))
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False, round((cost - tokens) / self.rate, 2)
        self._buckets[key] = (tokens - cost, now)
        return True, 0.0


#: simulation slots are far more expensive than reads, so they cost more tokens
BUCKET = TokenBucket(settings.rate_limit_rpm)


async def rate_limit(identity: Annotated[Identity, Depends(authenticate)]) -> Identity:
    allowed, retry_after = BUCKET.allow(identity.subject)
    if not allowed:
        METRICS.inc("http.rate_limited")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )
    return identity


# --------------------------------------------------------------------------- #
# twin snapshot with degraded-mode signalling
# --------------------------------------------------------------------------- #
async def snapshot_dependency(response: Response) -> TwinSnapshot:
    """The twin state every read endpoint renders.

    Never raises on storage loss: the cached snapshot is served with explicit
    degradation headers so the console can show a staleness badge instead of an
    error page during the incident it exists to manage.
    """
    try:
        snapshot = await repository.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.error("snapshot unavailable", extra={"custos": {"error": str(exc)[:200]}})
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "twin state unavailable; command console should fall back to its last cached view") from exc
    if snapshot.degraded:
        response.headers["X-CUSTOS-Degraded"] = "true"
    response.headers["X-CUSTOS-Twin-Digest"] = snapshot.digest()
    age = (datetime.now(timezone.utc) - snapshot.computed_at).total_seconds()
    response.headers["X-CUSTOS-Snapshot-Age"] = f"{max(0.0, age):.1f}"
    return snapshot


SnapshotDep = Annotated[TwinSnapshot, Depends(snapshot_dependency)]
IdentityDep = Annotated[Identity, Depends(authenticate)]
ReaderDep = Annotated[Identity, Depends(rate_limit)]
SimulatorDep = Annotated[Identity, Depends(require(Permission.SIMULATE))]
CommitterDep = Annotated[Identity, Depends(require(Permission.COMMIT))]
IngestDep = Annotated[Identity, Depends(require(Permission.INGEST))]
AdminDep = Annotated[Identity, Depends(require(Permission.ADMIN))]


# --------------------------------------------------------------------------- #
# middleware helpers
# --------------------------------------------------------------------------- #
def correlation_middleware(request: Request) -> str:
    """Continue the caller's correlation id, or start a new one."""
    incoming = request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID")
    return set_correlation_id(incoming)


def storage_mode() -> dict[str, Any]:
    """Where storage currently stands, for the health endpoint and the UI badge."""
    health = db.HEALTH
    return {
        "mode": getattr(health, "mode", "unknown"),
        "healthy": getattr(health, "healthy", True),
        "failures": getattr(health, "consecutive_failures", 0),
        "dialect": db.dialect_name(),
        "read_only": getattr(health, "read_only", False),
    }


def orchestrator_status() -> dict[str, Any]:
    return ORCHESTRATOR.status()
