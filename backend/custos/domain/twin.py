"""The Crisis Digital Twin as an in-memory, engine-facing value object.

The engines never touch ORM rows: they operate on an immutable-ish
:class:`TwinSnapshot`, which makes simulation *deterministic* (a run can be
replayed from its snapshot), *fast* (no I/O inside the cascade loop) and
*testable* (no database required to unit-test cascade reasoning).

A snapshot is a plain graph:

* ``nodes``    — canonical assets with live twin state
* ``edges``    — typed relationships (road -> hospital, substation -> telecom)
* ``zones``    — population units for equity accounting
* ``sensors``  — measurement points, already bound to nodes where possible
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping

from .enums import (
    AssetStatus,
    AssetType,
    FailureMode,
    RelationKind,
    SHELTER_TYPES,
    LIFELINE_TYPES,
    MOBILITY_TYPES,
    worst,
)


@dataclass(slots=True)
class AssetState:
    id: str
    name: str
    asset_type: AssetType
    criticality: float = 0.5
    lon: float = 0.0
    lat: float = 0.0
    elevation_m: float | None = None
    status: AssetStatus = AssetStatus.OK
    flood_depth_m: float = 0.0
    power_available: bool = True
    access_available: bool = True
    service_level: float = 1.0
    exposure_score: float = 0.0
    design_rainfall: float | None = None
    zone: str | None = None
    operator: str | None = None
    subtype: str | None = None
    geometry: dict[str, Any] = field(default_factory=dict)
    capacity: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    state_observed_at: datetime | None = None
    state_source: str | None = None
    state_confidence: float = 1.0

    # ---- derived helpers -------------------------------------------------
    @property
    def is_lifeline(self) -> bool:
        return self.asset_type in LIFELINE_TYPES

    @property
    def is_shelter(self) -> bool:
        return self.asset_type in SHELTER_TYPES

    @property
    def is_mobility(self) -> bool:
        return self.asset_type in MOBILITY_TYPES

    @property
    def flooded(self) -> bool:
        return self.flood_depth_m > 0.02

    @property
    def capacity_people(self) -> int:
        for key in ("capacity_people", "capacity", "occupancy"):
            value = self.capacity.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    @property
    def bed_count(self) -> int:
        for key in ("beds_total", "beds"):
            value = self.capacity.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    def copy(self) -> "AssetState":
        return replace(self, geometry=dict(self.geometry), capacity=dict(self.capacity), attributes=dict(self.attributes))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "asset_type": self.asset_type.value,
            "status": self.status.value,
            "criticality": self.criticality,
            "lon": self.lon,
            "lat": self.lat,
            "elevation_m": self.elevation_m,
            "flood_depth_m": round(self.flood_depth_m, 3),
            "power_available": self.power_available,
            "access_available": self.access_available,
            "service_level": round(self.service_level, 3),
            "exposure_score": round(self.exposure_score, 3),
            "zone": self.zone,
            "capacity": self.capacity,
            "state_confidence": self.state_confidence,
        }


@dataclass(slots=True)
class Edge:
    source_id: str
    target_id: str
    kind: RelationKind
    criticality: float = 0.5
    lead_time_min: int = 0
    attenuation: float = 1.0
    failure_mode: FailureMode = FailureMode.IMMEDIATE
    recovery_hours: float = 6.0
    redundancy_group: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind.value,
            "criticality": self.criticality,
            "lead_time_min": self.lead_time_min,
            "attenuation": self.attenuation,
            "failure_mode": self.failure_mode.value,
            "recovery_hours": self.recovery_hours,
            "redundancy_group": self.redundancy_group,
        }


@dataclass(slots=True)
class ZoneState:
    id: str
    name: str
    lon: float
    lat: float
    population: int = 0
    vulnerability_index: float = 0.3
    children_pct: float = 0.0
    elderly_pct: float = 0.0
    population_density: float = 0.0
    mean_elevation_m: float = 0.0

    @property
    def vulnerable_population(self) -> int:
        return int(self.population * (self.children_pct + self.elderly_pct))


@dataclass(slots=True)
class SensorState:
    id: str
    name: str
    metric: str
    unit: str
    lon: float
    lat: float
    asset_id: str | None = None
    zone: str | None = None
    baseline_median: float = 0.0
    baseline_mad: float = 0.0
    expected_min: float | None = None
    expected_max: float | None = None
    last_value: float | None = None
    last_observed_at: datetime | None = None
    is_active: bool = True

    @property
    def age_seconds(self) -> float | None:
        if self.last_observed_at is None:
            return None
        observed = self.last_observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - observed).total_seconds()


class TwinSnapshot:
    """Immutable-ish graph handed to every engine. Cheap to copy, cheap to diff."""

    __slots__ = (
        "nodes", "edges", "zones", "sensors", "computed_at", "city_id", "city_name", "degraded", "sources",
        "_out", "_in", "_adjacent",
    )

    def __init__(
        self,
        *,
        nodes: Mapping[str, AssetState] | None = None,
        edges: Iterable[Edge] = (),
        zones: Mapping[str, ZoneState] | None = None,
        sensors: Mapping[str, SensorState] | None = None,
        city_id: str = "kochi",
        city_name: str = "",
        degraded: bool = False,
        sources: Iterable[str] = (),
    ) -> None:
        self.nodes: dict[str, AssetState] = dict(nodes or {})
        self.edges: list[Edge] = list(edges)
        self.zones: dict[str, ZoneState] = dict(zones or {})
        self.sensors: dict[str, SensorState] = dict(sensors or {})
        self.computed_at = datetime.now(timezone.utc)
        self.city_id = city_id
        self.city_name = city_name
        self.degraded = degraded
        self.sources = tuple(sorted(set(sources)))
        self._out: dict[str, list[Edge]] | None = None
        self._in: dict[str, list[Edge]] | None = None
        self._adjacent: dict[str, set[str]] | None = None

    # ---- graph access ----------------------------------------------------
    def _index(self) -> None:
        if self._out is not None:
            return
        out: dict[str, list[Edge]] = {}
        inc: dict[str, list[Edge]] = {}
        adjacent: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.source_id not in self.nodes or edge.target_id not in self.nodes:
                continue  # dangling edge from a partially-synced mirror: ignore, never crash
            out.setdefault(edge.source_id, []).append(edge)
            inc.setdefault(edge.target_id, []).append(edge)
            adjacent.setdefault(edge.source_id, set()).add(edge.target_id)
            adjacent.setdefault(edge.target_id, set()).add(edge.source_id)
        self._out, self._in, self._adjacent = out, inc, adjacent

    def out_edges(self, node_id: str, kinds: Iterable[RelationKind] | None = None) -> list[Edge]:
        self._index()
        assert self._out is not None
        edges = self._out.get(node_id, [])
        if kinds is None:
            return edges
        allowed = set(kinds)
        return [e for e in edges if e.kind in allowed]

    def in_edges(self, node_id: str, kinds: Iterable[RelationKind] | None = None) -> list[Edge]:
        self._index()
        assert self._in is not None
        edges = self._in.get(node_id, [])
        if kinds is None:
            return edges
        allowed = set(kinds)
        return [e for e in edges if e.kind in allowed]

    def neighbours(self, node_id: str, hops: int = 1) -> set[str]:
        self._index()
        assert self._adjacent is not None
        seen = {node_id}
        frontier = {node_id}
        for _ in range(max(1, hops)):
            nxt: set[str] = set()
            for node in frontier:
                nxt |= self._adjacent.get(node, set())
            nxt -= seen
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        seen.discard(node_id)
        return seen

    # ---- selection -------------------------------------------------------
    def node(self, node_id: str) -> AssetState | None:
        return self.nodes.get(node_id)

    def of_type(self, *types: AssetType) -> list[AssetState]:
        wanted = set(types)
        return [n for n in self.nodes.values() if n.asset_type in wanted]

    @property
    def facilities(self) -> list[AssetState]:
        """Hospitals, substations, telecom, water - anything that serves people."""
        return [n for n in self.nodes.values() if n.is_lifeline]

    @property
    def shelters(self) -> list[AssetState]:
        return [n for n in self.nodes.values() if n.is_shelter]

    @property
    def roads(self) -> list[AssetState]:
        return [n for n in self.nodes.values() if n.is_mobility]

    def nearby(self, lon: float, lat: float, radius_m: float, *, types: Iterable[AssetType] | None = None) -> list[AssetState]:
        from ..fusion.normalize import haversine_m

        wanted = set(types) if types else None
        out = []
        for node in self.nodes.values():
            if wanted is not None and node.asset_type not in wanted:
                continue
            if haversine_m(lon, lat, node.lon, node.lat) <= radius_m:
                out.append(node)
        return out

    def zone_population(self, zone_id: str | None) -> int:
        if zone_id is None:
            return 0
        zone = self.zones.get(zone_id)
        return zone.population if zone else 0

    # ---- mutation (used by the simulation engine on a private copy) -------
    def apply_status(self, node_id: str, status: AssetStatus, *, source: str = "cascade") -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        node.status = worst(node.status, status)
        if node.status is AssetStatus.FAILED:
            node.service_level = 0.0
            node.access_available = False
        elif node.status is AssetStatus.IMPAIRED:
            node.service_level = min(node.service_level, 0.35)
        elif node.status is AssetStatus.DEGRADED:
            node.service_level = min(node.service_level, 0.7)
        node.state_source = source

    def fork(self) -> "TwinSnapshot":
        """Deep copy for a simulation branch. ~1 ms for a 400-node twin."""
        clone = TwinSnapshot(
            nodes={k: v.copy() for k, v in self.nodes.items()},
            edges=[replace(e, attributes=dict(e.attributes)) for e in self.edges],
            zones={k: replace(v) for k, v in self.zones.items()},
            sensors={k: replace(v) for k, v in self.sensors.items()},
            city_id=self.city_id,
            city_name=self.city_name,
            degraded=self.degraded,
            sources=self.sources,
        )
        clone.computed_at = self.computed_at
        return clone

    # ---- provenance ------------------------------------------------------
    def digest(self) -> str:
        """Stable fingerprint of the *state inputs* (not timestamps).

        Recorded with every simulation so a reviewer can prove exactly which twin
        state a scenario was computed against - the difference between "the model
        was wrong" and "the model was fed stale data".
        """
        payload = [
            (n.id, n.status.value, round(n.flood_depth_m, 3), n.power_available, n.access_available, round(n.service_level, 3))
            for n in sorted(self.nodes.values(), key=lambda x: x.id)
        ]
        blob = json.dumps(payload, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for node in self.nodes.values():
            by_type[node.asset_type.value] = by_type.get(node.asset_type.value, 0) + 1
            by_status[node.status.value] = by_status.get(node.status.value, 0) + 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "zones": len(self.zones),
            "sensors": len(self.sensors),
            "by_type": by_type,
            "by_status": by_status,
            "computed_at": self.computed_at.isoformat(),
            "digest": self.digest(),
            "sources": list(self.sources),
            "degraded": self.degraded,
        }

    def iterate_nodes(self) -> Iterator[AssetState]:
        return iter(self.nodes.values())
