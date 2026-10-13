"""WebSocket fan-out for the command console.

Properties the console depends on, in order of importance:

1. **A dropped tab must not slow the city down.** Each subscriber has a bounded
   queue; when it fills, the *oldest* event is dropped and counted. The producers
   (pipeline, event bus) never block on a slow browser.
2. **A reconnect must not lose the picture.** Every event carries a monotonic
   ``seq`` from the bus ring, and a client may resume with ``?since_seq=``.
   A client that has been away longer than the ring gets a full snapshot instead
   of a silent gap.
3. **A dead link must be visible.** Heartbeats every 15 s carry the twin digest
   and freshness, so a console that has stopped receiving updates says so instead
   of showing yesterday's flood as if it were live.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..config import settings
from ..domain.enums import Role, ROLE_PERMISSIONS, Permission
from ..fusion import repository
from ..observability import get_logger
from ..realtime.bus import BUS, Topics
from ..services.pipeline import ORCHESTRATOR
from .deps import _decode_token, _configured_api_keys

log = get_logger("api.ws")
router = APIRouter()

HEARTBEAT_S = 15.0
MAX_TOPICS = 24


async def _authorise(websocket: WebSocket) -> Role | None:
    """Authenticate a socket before accepting it.

    A browser cannot set headers on a WebSocket handshake, so the token arrives
    either as a subprotocol-ish query parameter (same-origin only, over TLS in a
    deployed EOC) or as the API key. Read-only roles are the default.
    """
    token = websocket.query_params.get("token") or ""
    api_key = websocket.query_params.get("api_key") or websocket.headers.get("x-api-key") or ""
    if api_key:
        for key, role in _configured_api_keys().items():
            if key == api_key:
                return role
    if token:
        try:
            claims = _decode_token(token)
        except Exception:  # noqa: BLE001
            return None
        try:
            return Role(str(claims.get("role") or "viewer"))
        except ValueError:
            return Role.VIEWER
    return None


@router.websocket("/stream")
async def stream(
    websocket: WebSocket,
    since_seq: int = Query(default=0, ge=0),
    topics: str | None = Query(default=None, description="comma-separated topic filter"),
) -> None:
    role = await _authorise(websocket)
    if role is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="authentication required")
        return
    if Permission.READ not in ROLE_PERMISSIONS.get(role, frozenset()):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="role may not read the twin")
        return

    wanted = [topic.strip() for topic in (topics or "").split(",") if topic.strip()][:MAX_TOPICS] or None
    await websocket.accept(subprotocol=None)
    subscriber_id = f"ws-{id(websocket)}"
    subscription = BUS.subscribe(subscriber_id, wanted)
    client = websocket.client
    log.info("websocket connected", extra={"custos": {"subscriber": subscriber_id, "role": role.value, "peer": client.host if client else None, "topics": wanted}})

    try:
        # 1. orientation: what the client missed, then where the world stands now
        missed = BUS.replay(since_seq, topics=wanted, limit=300) if since_seq else []
        await websocket.send_json(
            {
                "type": "hello",
                "at": datetime.now(timezone.utc).isoformat(),
                "role": role.value,
                "topics": BUS.stats().get("topics"),
                "subscribed": wanted or "all",
                "resumed_from_seq": since_seq,
                "replayed": len(missed),
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        )
        for event in missed:
            await websocket.send_json({"type": "event", **event.to_wire()})
        try:
            snapshot = await repository.get_snapshot()
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "digest": snapshot.digest(),
                    "degraded": snapshot.degraded,
                    "stats": snapshot.stats(),
                    "stages": (ORCHESTRATOR.status().get("last_cycle") or {}).get("stages", []),
                    "prediction": snapshot.attributes.get("prediction", {}),
                }
            )
        except Exception:  # noqa: BLE001 - the stream survives the database
            await websocket.send_json({"type": "snapshot", "degraded": True, "detail": "twin state temporarily unavailable"})

        # 2. steady state: bus events out, client control messages in
        async def pump() -> None:
            async for event in subscription.drain(timeout=1.0):
                if websocket.client_state.name != "CONNECTED":
                    return
                await websocket.send_json({"type": "event", **event.to_wire()})

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                if websocket.client_state.name != "CONNECTED":
                    return
                snapshot_age: float | None = None
                try:
                    cached = await repository.get_snapshot()
                    snapshot_age = (datetime.now(timezone.utc) - cached.computed_at).total_seconds()
                except Exception:  # noqa: BLE001
                    pass
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "bus": BUS.stats(),
                        "snapshot_age_s": None if snapshot_age is None else round(snapshot_age, 1),
                        "degraded": ORCHESTRATOR.status().get("last_cycle", {}).get("degraded", False) if ORCHESTRATOR.status().get("last_cycle") else False,
                    }
                )

        async def listen() -> None:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "detail": "control messages must be JSON"})
                    continue
                action = str(message.get("action") or "")
                if action == "ping":
                    await websocket.send_json({"type": "pong", "at": datetime.now(timezone.utc).isoformat()})
                elif action == "subscribe":
                    subscription.topics = frozenset(str(t) for t in message.get("topics") or []) or None
                    await websocket.send_json({"type": "subscribed", "topics": sorted(subscription.topics) if subscription.topics else "all"})
                elif action == "replay":
                    events = BUS.replay(int(message.get("since_seq") or 0), topics=subscription.topics, limit=300)
                    for event in events:
                        await websocket.send_json({"type": "event", **event.to_wire()})
                    await websocket.send_json({"type": "replayed", "count": len(events)})
                elif action == "drill":
                    # an operator can drive the telemetry replay from the console
                    ORCHESTRATOR.set_scenario_rain(float(message.get("rainfall_mm_h") or 0.0))
                    await websocket.send_json({"type": "drill", "rainfall_mm_h": ORCHESTRATOR.status()["scenario_rain_mm_h"]})
                else:
                    await websocket.send_json({"type": "error", "detail": f"unknown action {action!r}"})

        tasks = [asyncio.create_task(pump()), asyncio.create_task(heartbeat()), asyncio.create_task(listen())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        log.info("websocket disconnected", extra={"custos": {"subscriber": subscriber_id}})
    except Exception as exc:  # noqa: BLE001 - an unexpected socket error must not take the app down
        log.warning("websocket error", extra={"custos": {"subscriber": subscriber_id, "error": str(exc)[:200]}})
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:  # noqa: BLE001
            pass
    finally:
        BUS.unsubscribe(subscriber_id)
