"""In-process event bus: the spine between engines and every command console.

Properties that matter during a crisis
--------------------------------------
* **Never blocks a producer.** A stalled browser tab, a slow websocket or a dead
  subscriber must not be able to slow down the simulation engine. Each subscriber
  owns a bounded queue; on overflow the *oldest* event is dropped and the drop is
  counted, so the console can display "N events dropped - resynchronising" rather
  than silently showing a wrong picture.
* **Replayable.** A ring buffer keeps the most recent events per topic so a
  reconnecting console can resume from its last seen sequence number.
* **Durable.** :func:`publish` mirrors to the ``events`` table asynchronously by
  default; a failure to persist never blocks delivery (degraded mode).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Deque, Iterable, Mapping

from ..domain.enums import EventKind
from ..observability import METRICS, get_logger

log = get_logger("realtime.bus")

MAX_QUEUE = 512
RING_SIZE = 2048


@dataclass(slots=True)
class Event:
    seq: int
    kind: str
    topic: str
    payload: dict[str, Any]
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = "-"

    def to_wire(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "topic": self.topic,
            "at": self.at.isoformat(),
            "payload": self.payload,
            "correlation_id": self.correlation_id,
        }


class Subscription:
    """A bounded, drop-oldest subscription with usage accounting."""

    def __init__(self, subscriber_id: str, topics: frozenset[str] | None) -> None:
        self.id = subscriber_id
        self.topics = topics
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=MAX_QUEUE)
        self.dropped = 0
        self.delivered = 0
        self.created_at = time.time()
        self.last_delivery_at: float | None = None
        self.closed = False

    def matches(self, event: Event) -> bool:
        return self.topics is None or "*" in self.topics or event.topic in self.topics

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # drop the oldest, keep the newest: for situational awareness the most
            # recent state is always more valuable than a queued historic frame
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.queue.task_done()
            self.dropped += 1
            METRICS.inc("bus.dropped", topic=event.topic)
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)

    @property
    def depth(self) -> int:
        return self.queue.qsize()

    async def drain(self, timeout: float | None = None) -> AsyncIterator[Event]:
        while not self.closed or not self.queue.empty():
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return
            self.delivered += 1
            self.last_delivery_at = time.time()
            yield event

    def close(self) -> None:
        self.closed = True
        # unblock any pending consumer
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(
                Event(seq=-1, kind=EventKind.SYSTEM.value, topic="__closed__", payload={"reason": "subscription closed"})
            )


class EventBus:
    def __init__(self, *, ring_size: int = RING_SIZE) -> None:
        self._seq = itertools.count(1)
        self._subs: dict[str, Subscription] = {}
        self._ring: Deque[Event] = deque(maxlen=ring_size)
        self._lock = asyncio.Lock()
        self.published = 0
        self.persist: Any = None  # set by the app to a coroutine mirrored to Postgres

    # ------------------------------------------------------------------ #
    @property
    def next_seq(self) -> int:
        return next(self._seq)

    async def publish(
        self,
        topic: str,
        payload: Mapping[str, Any],
        *,
        kind: EventKind | str = EventKind.SYSTEM,
        correlation_id: str = "-",
        persist: bool = True,
    ) -> Event:
        event = Event(
            seq=self.next_seq,
            kind=kind.value if isinstance(kind, EventKind) else str(kind),
            topic=topic,
            payload=dict(payload),
            correlation_id=correlation_id,
        )
        async with self._lock:
            self._ring.append(event)
            self.published += 1
            targets = [s for s in self._subs.values() if not s.closed and s.matches(event)]
        for sub in targets:
            sub.offer(event)
        METRICS.inc("bus.published", topic=topic)
        if persist and self.persist is not None:
            # fire-and-forget: a database hiccup must not stall the engines
            asyncio.create_task(self._safe_persist(event))
        return event

    async def _safe_persist(self, event: Event) -> None:
        try:
            await self.persist(event)
        except Exception as exc:  # noqa: BLE001
            METRICS.inc("bus.persist_failed")
            log.warning("event persistence failed", extra={"custos": {"topic": event.topic, "error": str(exc)[:200]}})

    # ------------------------------------------------------------------ #
    def subscribe(self, subscriber_id: str, topics: Iterable[str] | None = None) -> Subscription:
        topic_set = frozenset(topics) if topics else None
        sub = Subscription(subscriber_id, topic_set)
        self._subs[subscriber_id] = sub
        METRICS.inc("bus.subscribers")
        return sub

    def unsubscribe(self, subscriber_id: str) -> None:
        sub = self._subs.pop(subscriber_id, None)
        if sub is not None:
            sub.close()
            METRICS.inc("bus.unsubscribers")

    def replay(self, from_seq: int, topics: Iterable[str] | None = None, limit: int = 500) -> list[Event]:
        topic_set = frozenset(topics) if topics else None
        out = [
            e
            for e in self._ring
            if e.seq > from_seq and (topic_set is None or "*" in topic_set or e.topic in topic_set)
        ]
        return out[-limit:]

    def stats(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "ring_size": len(self._ring),
            "oldest_seq": self._ring[0].seq if self._ring else None,
            "latest_seq": self._ring[-1].seq if self._ring else None,
            "subscribers": [
                {
                    "id": s.id,
                    "topics": sorted(s.topics) if s.topics else ["*"],
                    "depth": s.depth,
                    "dropped": s.dropped,
                    "delivered": s.delivered,
                    "age_s": round(time.time() - s.created_at, 1),
                }
                for s in self._subs.values()
            ],
        }


BUS = EventBus()


# --------------------------------------------------------------------------- #
# topics - centralised so the frontend, engines and tests cannot drift apart
# --------------------------------------------------------------------------- #
class Topics:
    INCIDENT_NEW = "incident.new"
    INCIDENT_UPDATE = "incident.update"
    SENSOR_READING = "sensor.reading"
    ANOMALY = "sensor.anomaly"
    ASSET_STATE = "asset.state"
    TWIN_SNAPSHOT = "twin.snapshot"
    SIM_PROGRESS = "simulation.progress"
    SIM_COMPLETE = "simulation.complete"
    RECOMMENDATION = "recommendation.new"
    TASK = "task.update"
    SYSTEM = "system.status"
    ALERT = "system.alert"
    AUDIT = "audit.entry"

    @classmethod
    def all(cls) -> list[str]:
        return [v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str)]


class HeartbeatPublisher:
    """Keeps consoles honest: proves liveness even when nothing is happening."""

    def __init__(self, bus: EventBus, interval_s: float = 15.0) -> None:
        self.bus = bus
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._snapshot_provider: Any = None
        self.ticks = 0

    def set_snapshot_provider(self, provider: Any) -> None:
        self._snapshot_provider = provider

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            self.ticks += 1
            payload: dict[str, Any] = {"tick": self.ticks, "bus": self.bus.stats()}
            if self._snapshot_provider is not None:
                with contextlib.suppress(Exception):
                    payload["summary"] = await self._snapshot_provider()
            await self.bus.publish(Topics.SYSTEM, payload, kind=EventKind.SYSTEM, persist=False)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="custos-heartbeat")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


@dataclass
class TopicLag:
    topic: str
    produced: int
    consumed: int

    @property
    def lag(self) -> int:
        return max(0, self.produced - self.consumed)


class LagTracker:
    """Small helper used by the health endpoint to expose pipeline backlog."""

    def __init__(self) -> None:
        self.counters: "OrderedDict[str, TopicLag]" = OrderedDict()

    def produced(self, topic: str) -> None:
        entry = self.counters.setdefault(topic, TopicLag(topic, 0, 0))
        entry.produced += 1

    def consumed(self, topic: str) -> None:
        entry = self.counters.setdefault(topic, TopicLag(topic, 0, 0))
        entry.consumed += 1

    def snapshot(self) -> dict[str, int]:
        return {k: v.lag for k, v in self.counters.items()}
