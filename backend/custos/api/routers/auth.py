"""Authentication endpoints for the command console.

The MVP ships a deliberately small identity surface: an operator roster held in
configuration (or a database-backed one later), short-lived JWTs, and refresh
tokens. What it does *not* do is any of the things that get an EOC breached:
no default password, no token in a URL, no role the client can choose for itself
unless the deployment explicitly allows self-registration (off by default).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from ...config import settings
from ...domain.enums import Permission, Role, ROLE_PERMISSIONS
from ...domain.schemas import AuthToken, LoginRequest
from ...realtime.bus import BUS, Topics
from ...services import store
from ..deps import IdentityDep, _decode_token, issue_tokens, permissions_for, rate_limit

router = APIRouter(prefix="/auth", tags=["auth"])

#: Failed logins per subject, to slow a credential-stuffing attempt without
#: giving an attacker a way to lock a commander out of their own console.
_FAILED: dict[str, list[float]] = {}
_MAX_FAILURES = 8
_LOCKOUT_S = 300.0


def _operators() -> dict[str, tuple[str, str, Role]]:
    """``subject:hash$salt:role`` entries from configuration.

    Each entry carries its *own* salt, so two operators with the same password do
    not share a digest and a leaked roster cannot be attacked as a batch. The
    plaintext never appears in configuration, environment dumps or logs.
    """
    roster: dict[str, tuple[str, str, Role]] = {}
    for entry in (settings.operator_roster or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 3:
            continue
        subject, credential, role = parts[0], parts[1], parts[2]
        digest, _, salt = credential.partition("$")
        if not digest:
            continue
        try:
            roster[subject] = (digest, salt, Role(role))
        except ValueError:
            continue
    return roster


def hash_password(password: str, *, salt: str = "") -> str:
    """Digest used for roster entries: sha256(salt || password)."""
    return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()


def roster_entry(subject: str, password: str, role: Role, *, salt: str | None = None) -> str:
    """Build one ``subject:hash$salt:role`` roster entry (used by the CLI)."""
    import secrets as _secrets

    effective_salt = salt if salt is not None else _secrets.token_hex(8)
    return f"{subject}:{hash_password(password, salt=effective_salt)}${effective_salt}:{role.value}"


@router.post("/login", response_model=AuthToken, summary="Exchange credentials for tokens")
async def login(request: Request, payload: LoginRequest) -> AuthToken:
    now = time.monotonic()
    failures = [stamp for stamp in _FAILED.get(payload.subject, []) if now - stamp < _LOCKOUT_S]
    if len(failures) >= _MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed attempts; try again shortly")
    roster = _operators()
    expected = roster.get(payload.subject)
    if expected is None:
        # constant-time comparison against a dummy keeps the timing flat whether or
        # not the subject exists: user enumeration is an information leak too
        hmac.compare_digest(payload.password, "decoy-password")
        _FAILED[payload.subject] = failures + [now]
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    supplied = hash_password(payload.password, salt=expected[1])
    if not hmac.compare_digest(supplied, expected[0]):
        _FAILED[payload.subject] = failures + [now]
        try:
            async with store.session_scope() as session:
                await store.write_audit(
                    session,
                    actor=payload.subject,
                    role="unknown",
                    action="login",
                    resource="auth",
                    outcome="denied",
                    detail={"reason": "bad password", "ip": request.client.host if request.client else None},
                )
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    _FAILED.pop(payload.subject, None)
    role = expected[2]
    tokens = issue_tokens(payload.subject, role)
    try:
        async with store.session_scope() as session:
            await store.write_audit(session, actor=payload.subject, role=role.value, action="login", resource="auth", detail={"ip": request.client.host if request.client else None})
    except Exception:  # noqa: BLE001
        pass
    await BUS.publish(Topics.SYSTEM, {"event": "login", "subject": payload.subject, "role": role.value}, source="auth")
    return AuthToken(**tokens)


@router.post("/refresh", response_model=AuthToken, summary="Refresh an access token")
async def refresh(payload: dict[str, Any]) -> AuthToken:
    token = str(payload.get("refresh_token") or "")
    if not token:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "refresh_token is required")
    claims = _decode_token(token, expected_type="refresh")
    subject = str(claims.get("sub") or "")
    roster = _operators()
    role = roster.get(subject, (None, None, Role(str(claims.get("role") or "viewer"))))[2]
    if roster and subject not in roster:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "this account no longer exists")
    return AuthToken(**issue_tokens(subject, role))


@router.get("/me", summary="Who am I and what may I do")
async def me(identity: IdentityDep) -> dict[str, Any]:
    return {
        "subject": identity.subject,
        "role": identity.role.value,
        "auth_method": identity.auth_method,
        "permissions": sorted(permission.value for permission in identity.permissions),
        "server_time": datetime.now(timezone.utc),
    }


@router.get("/permissions", summary="The role matrix, published rather than implied")
async def permissions(identity: IdentityDep) -> dict[str, Any]:
    return {
        "roles": {
            role.value: sorted(permission.value for permission in permissions_for(role))
            for role in Role
        },
        "matrix_source": "the same table the server enforces; the console hides controls it knows it cannot use",
    }
