"""Urban flood model for a tidal backwater city.

What the model captures (and why each term exists)
--------------------------------------------------
Kochi does not flood like a river town. It floods when four things coincide:

1. **Rainfall intensity** exceeds the drainage network's design capacity
   (the network is designed for roughly 40-50 mm/h).
2. **Canal stage** rises above bankfull, so water leaves the canal and takes the
   road surface as its channel.
3. **Backwater / tide stage** rises above the canal stage, so the *gravity
   outfalls stop working*. Drainage then depends entirely on pumps.
4. **Pumps lose power** - the very substations that feed them are frequently in
   the same low-lying pocket.

Structure
---------
* ``WaterBody``  - lumped store per canal / river reach / backwater, with a
  two-stage geometry: the surveyed channel fills first, then water spreads over
  the floodplain. Surcharge therefore becomes *street depth* rather than an
  absurd stage, which is the quantity an operations cell actually needs.
* **Weir overflow** (``Q = 1.7 · L · H^1.5``, broad-crested) turns "canal is
  0.4 m over bankfull" into a discharge onto the adjacent streets.
* **Gravity outfall** throttles as the receiving water rises, and stops entirely
  when it exceeds the canal stage - the tide-lock that makes the city flood for
  days rather than hours.
* ``ZoneHydrology`` - distributed street ponding per census zone, fed by direct
  rainfall and canal overflow, drained by pumps whose availability depends on
  the power graph.

It is *not* a 2-D hydrodynamic solver and does not pretend to be. It is
calibrated to be right about ordering and timing - which road closes first, and
when - because that is what a commander acts on. ``ASSUMPTIONS`` names every
simplification so a hydrology team can replace the internals behind the same
interface (:meth:`HydroModel.step`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import networkx as nx

from ..domain.enums import AssetStatus, AssetType, HazardType, InterventionType, RelationKind
from ..domain.twin import AssetState, TwinSnapshot
from ..fusion.normalize import haversine_m
from ..observability import get_logger

log = get_logger("engines.hydro")

ASSUMPTIONS = [
    "Lumped (not 2-D) mass balance per water body; surface area constant over the modelled stage band.",
    "Channel and floodplain areas are taken from the operator's surveyed section when supplied; otherwise estimated from reach length and navigable width.",
    "Street ponding is aggregated per census zone (with a concentration factor of 0.28 for the ponding fraction of the ward), then attributed to assets by ground elevation and distance to the receiving water body.",
    "Zone drainage capacity is derived from the ward's declared design rainfall (mm/h): the network is sized to pass that intensity, which is why the model shows a sharp knee around it.",
    "Infiltration, sewer leakage and minor local drainage are represented by a single calibrated loss coefficient (0.18 of rainfall).",
    "Tidal modulation uses an M2 principal constituent (12.42 h) plus the operator-supplied surge.",
    "Overland transfer between zones is not modelled; each zone drains to its assigned outfall.",
    "Canal overtopping uses a broad-crested weir relation (Cw = 1.7) across the declared channel width.",
    "Recovery after rainfall stops is pump-limited, which is the dominant real-world constraint in Kochi.",
]

SECONDS_PER_HOUR = 3600.0
TIDE_PERIOD_H = 12.42
RAINWATER_LOSS_COEFFICIENT = 0.18
WEIR_COEFFICIENT = 1.7
DEFAULT_ZONE_AREA_KM2 = 2.6
DEFAULT_DESIGN_RAINFALL_MM_H = 40.0
#: runoff concentrates: only a fraction of a ward actually ponds, which is why
#: "ward-average" depths look harmless while a 300 m stretch of road is knee-deep
PONDING_CONCENTRATION = 0.28
MAX_DEPTH_M = 6.0


@dataclass(slots=True)
class WaterBody:
    """A lumped hydraulic store."""

    asset_id: str
    name: str
    kind: str  # canal | river | backwater
    zone: str | None
    invert_elevation_m: float
    bankfull_level_m: float
    surface_area_m2: float
    floodplain_area_m2: float
    channel_width_m: float
    catchment_km2: float
    outfall_asset_id: str | None = None
    gravity_capacity_m3s: float = 12.0
    pumped_capacity_m3s: float = 0.0
    stage_m: float = 0.0
    storage_m3: float = 0.0
    inflow_m3s: float = 0.0
    upstream_m3s: float = 0.0
    gravity_m3s: float = 0.0
    pumped_now_m3s: float = 0.0
    overflow_m3s: float = 0.0
    blocked: bool = False
    observations: list[str] = field(default_factory=list)

    @property
    def outflow_m3s(self) -> float:
        return self.gravity_m3s + self.pumped_now_m3s + self.overflow_m3s

    @property
    def freeboard_m(self) -> float:
        return round(self.bankfull_level_m - self.stage_m, 3)

    @property
    def above_bankfull_m(self) -> float:
        return round(max(0.0, self.stage_m - self.bankfull_level_m), 3)

    @property
    def bankfull_storage_m3(self) -> float:
        return max(0.0, self.bankfull_level_m - self.invert_elevation_m) * self.surface_area_m2

    def stage_for_storage(self, storage_m3: float) -> float:
        """Two-stage geometry: channel first, then floodplain."""
        channel = self.bankfull_storage_m3
        if storage_m3 <= channel:
            return self.invert_elevation_m + storage_m3 / self.surface_area_m2
        excess = storage_m3 - channel
        return self.bankfull_level_m + excess / max(1.0, self.floodplain_area_m2)

    def storage_for_stage(self, stage_m: float) -> float:
        if stage_m <= self.bankfull_level_m:
            return max(0.0, (stage_m - self.invert_elevation_m) * self.surface_area_m2)
        return self.bankfull_storage_m3 + (stage_m - self.bankfull_level_m) * max(1.0, self.floodplain_area_m2)


@dataclass(slots=True)
class ZoneHydrology:
    zone_id: str
    name: str
    area_km2: float
    mean_elevation_m: float
    population: int
    drainage_capacity_m3s: float
    design_rainfall_mm_h: float
    outfall_asset_id: str | None
    ponding_m: float = 0.0
    rainfall_mm_h: float = 0.0
    overland_in_m3s: float = 0.0
    drained_m3: float = 0.0
    inflow_m3s: float = 0.0

    @property
    def over_capacity(self) -> bool:
        return self.inflow_m3s > self.drainage_capacity_m3s


@dataclass(slots=True)
class Forcing:
    """Scenario forcing for one timestep, already ensemble-perturbed."""

    rainfall_mm_h: float = 0.0
    rainfall_by_zone: dict[str, float] = field(default_factory=dict)
    surge_m: float = 0.0
    release_m3s: dict[str, float] = field(default_factory=dict)
    injections: dict[str, float] = field(default_factory=dict)
    pump_multiplier: dict[str, float] = field(default_factory=dict)
    drainage_multiplier: dict[str, float] = field(default_factory=dict)
    blocked_bodies: set[str] = field(default_factory=set)


@dataclass(slots=True)
class HydroDiagnostics:
    tide_m: float = 0.0
    rainfall_mm_h: float = 0.0
    tide_locked_bodies: list[str] = field(default_factory=list)
    overtopping_bodies: list[str] = field(default_factory=list)
    total_pumped_m3s: float = 0.0
    total_gravity_m3s: float = 0.0
    notes: list[str] = field(default_factory=list)


class HydroModel:
    """Lumped rainfall-runoff + canal/backwater + street ponding model."""

    def __init__(self, snapshot: TwinSnapshot, *, t0: datetime | None = None, tide_phase_h: float = 3.1) -> None:
        self.snapshot = snapshot
        self.t0 = t0 or datetime.now(timezone.utc)
        self.tide_phase_h = tide_phase_h
        self.tide_mean_m = 1.02
        self.tide_amplitude_m = 0.62
        self.water_bodies: dict[str, WaterBody] = {}
        self.zones: dict[str, ZoneHydrology] = {}
        self.asset_outfall: dict[str, str | None] = {}
        self.asset_ground: dict[str, float] = {}
        self.zone_overland_sources: dict[str, list[tuple[str, float]]] = {}
        self._distance_cache: dict[tuple[str, str], float] = {}
        self._order: list[str] = []
        self._build()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        nodes = self.snapshot.nodes

        for node in nodes.values():
            if node.asset_type not in {AssetType.CANAL, AssetType.RIVER_GAUGE}:
                continue
            capacity = node.capacity
            bankfull = float(capacity.get("bankfull_level_m") or (node.elevation_m or 1.0) + 0.9)
            # the declared bankfull level is an absolute water level (AMSL); the
            # asset's own elevation is the ground beside the channel
            invert = float(capacity.get("invert_elevation_m") or max(0.15, bankfull - 1.7))
            length_m = self._linear_length_m(node)
            default_width = 90.0 if node.subtype == "backwater" else (45.0 if node.asset_type is AssetType.RIVER_GAUGE else 14.0)
            width_m = float(capacity.get("width_m") or default_width)
            area = max(2_000.0, length_m * width_m)
            floodplain_width = float(capacity.get("floodplain_width_m") or (320.0 if node.asset_type is AssetType.RIVER_GAUGE else 180.0))
            body = WaterBody(
                asset_id=node.id,
                name=node.name,
                kind="backwater" if node.subtype == "backwater" else ("river" if node.asset_type is AssetType.RIVER_GAUGE else "canal"),
                zone=node.zone,
                invert_elevation_m=invert,
                bankfull_level_m=bankfull,
                surface_area_m2=area,
                floodplain_area_m2=max(area * 4.0, length_m * floodplain_width),
                channel_width_m=width_m,
                catchment_km2=float(capacity.get("catchment_km2") or max(1.1, length_m / 1000.0 * 1.15)),
                gravity_capacity_m3s=float(capacity.get("design_capacity_m3s") or max(4.5, width_m * 0.55)),
            )
            stage = float(capacity.get("initial_stage_m") or min(bankfull - 0.75, invert + 1.15))
            body.stage_m = max(invert, min(stage, bankfull + 0.6))
            body.storage_m3 = body.storage_for_stage(body.stage_m)
            body.blocked = bool(capacity.get("blocked"))
            self.water_bodies[node.id] = body

        # outfalls: backwater is the terminal sink; canals/rivers discharge to it
        backwaters = [b.asset_id for b in self.water_bodies.values() if b.kind == "backwater"]
        for body in self.water_bodies.values():
            if body.kind == "backwater":
                body.outfall_asset_id = None
                continue
            body.outfall_asset_id = self._nearest_of(body.asset_id, backwaters, max_m=40_000)
        for edge in self.snapshot.edges:
            if edge.kind is RelationKind.FEEDS and edge.source_id in self.water_bodies and edge.target_id in self.water_bodies:
                self.water_bodies[edge.source_id].outfall_asset_id = edge.target_id

        # pumps add forced drainage where the twin declares DRAINS edges
        for node in nodes.values():
            if node.asset_type is not AssetType.PUMP_STATION:
                continue
            for edge in self.snapshot.out_edges(node.id, kinds=[RelationKind.DRAINS]):
                target = self.water_bodies.get(edge.target_id)
                if target is not None:
                    target.pumped_capacity_m3s += float(edge.attributes.get("capacity_m3s") or node.capacity.get("pump_rate_m3s") or 0.0)

        # zones
        zone_design: dict[str, float] = {}
        for zone_id, zone in self.snapshot.zones.items():
            zone_asset = next((n for n in nodes.values() if n.zone == zone_id and n.design_rainfall), None)
            if zone_asset is not None:
                zone_design[zone_id] = float(zone_asset.design_rainfall)
        for zone in self.snapshot.zones.values():
            outfall = self._nearest_of(
                f"zone:{zone.id}", list(self.water_bodies.keys()), point=(zone.lon, zone.lat), max_m=3_500
            )
            pumped = sum(
                float(node.capacity.get("pump_rate_m3s") or 0.0)
                for node in nodes.values()
                if node.asset_type is AssetType.PUMP_STATION and node.zone == zone.id
            )
            # a zone whose pumps drain a canal is still drained locally; the pump
            # contribution is counted once, on the canal side, to avoid double counting
            area_km2 = self._zone_area_km2(zone.population, zone.population_density)
            design_mm_h = float(zone_design.get(zone.id, DEFAULT_DESIGN_RAINFALL_MM_H))
            # the sewer network is sized to pass the design intensity; pumps are
            # added capacity on top of it, never a substitute for it
            design_capacity = area_km2 * design_mm_h * 1000.0 / SECONDS_PER_HOUR * (1.0 - RAINWATER_LOSS_COEFFICIENT)
            self.zones[zone.id] = ZoneHydrology(
                zone_id=zone.id,
                name=zone.name,
                area_km2=area_km2,
                mean_elevation_m=zone.mean_elevation_m,
                population=zone.population,
                drainage_capacity_m3s=design_capacity + 0.35 * pumped,
                design_rainfall_mm_h=design_mm_h,
                outfall_asset_id=outfall,
            )

        # per-asset ground elevation and receiving water body
        for node in nodes.values():
            self.asset_ground[node.id] = float(
                node.elevation_m if node.elevation_m is not None else self._zone_elevation(node.zone)
            )
            self.asset_outfall[node.id] = self._receiving_body(node)

        # overland overflow routing: which zones receive a body's overtopping
        for body in self.water_bodies.values():
            node = nodes.get(body.asset_id)
            if node is None:
                continue
            for zone in self.snapshot.zones.values():
                distance = haversine_m(node.lon, node.lat, zone.lon, zone.lat)
                if distance > 2_600:
                    continue
                weight = max(0.05, 1.0 - distance / 2_600.0)
                self.zone_overland_sources.setdefault(zone.id, []).append((body.asset_id, weight))
        for zone_id, sources in self.zone_overland_sources.items():
            total = sum(w for _, w in sources) or 1.0
            self.zone_overland_sources[zone_id] = [(bid, w / total) for bid, w in sources]

        self._order = self._hydraulic_order()

    @staticmethod
    def _zone_area_km2(population: int, density: float) -> float:
        if density and density > 0:
            return max(0.4, population / density)
        return DEFAULT_ZONE_AREA_KM2

    def _receiving_body(self, node: AssetState) -> str | None:
        """Which water body physically receives this asset's drainage."""
        best: str | None = None
        best_score = float("inf")
        for body in self.water_bodies.values():
            other = self.snapshot.nodes.get(body.asset_id)
            if other is None:
                continue
            distance = haversine_m(node.lon, node.lat, other.lon, other.lat)
            # connected hydraulic objects get a strong preference over mere proximity
            linked = any(
                edge.target_id == body.asset_id and edge.kind in {RelationKind.MONITORS, RelationKind.DRAINS, RelationKind.FEEDS}
                for edge in self.snapshot.out_edges(node.id)
            ) or any(
                edge.source_id == body.asset_id and edge.kind in {RelationKind.FEEDS, RelationKind.DRAINS}
                for edge in self.snapshot.in_edges(node.id)
            )
            score = distance * (0.35 if linked else 1.0)
            if score < best_score:
                best, best_score = body.asset_id, score
        limit = 3_000.0 if best and self.water_bodies[best].kind == "canal" else 6_000.0
        if best_score > limit:
            return None
        return best

    def _hydraulic_order(self) -> list[str]:
        """Topological order over FEEDS so a downstream body sees the current
        inflow of its upstream neighbour instead of a stale value."""
        graph = nx.DiGraph()
        graph.add_nodes_from(self.water_bodies)
        for edge in self.snapshot.edges:
            if edge.kind is RelationKind.FEEDS and edge.source_id in self.water_bodies and edge.target_id in self.water_bodies:
                graph.add_edge(edge.source_id, edge.target_id)
        try:
            return list(nx.topological_sort(graph))
        except nx.NetworkXUnfeasible:
            # a hydraulic loop (rare, but real in backwater networks): condense the
            # strongly connected components and fall back to elevation ordering
            condensed = nx.condensation(graph)
            order: list[str] = []
            for component in nx.topological_sort(condensed):
                order.extend(sorted(condensed.nodes[component]["members"], key=lambda bid: -self.water_bodies[bid].invert_elevation_m))
            return order

    @staticmethod
    def _linear_length_m(node: AssetState) -> float:
        geometry = node.geometry or {}
        if geometry.get("type") == "LineString":
            coords = geometry.get("coordinates") or []
            total = 0.0
            for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
                total += haversine_m(lon1, lat1, lon2, lat2)
            if total > 0:
                return total
        return float(node.capacity.get("length_m") or 1_400.0)

    def _zone_elevation(self, zone_id: str | None) -> float:
        zone = self.snapshot.zones.get(zone_id or "")
        return float(zone.mean_elevation_m) if zone else 3.0

    def _nearest_of(
        self,
        asset_id: str,
        candidates: Iterable[str],
        *,
        point: tuple[float, float] | None = None,
        max_m: float = 60_000.0,
    ) -> str | None:
        if point is None:
            state = self.snapshot.nodes.get(asset_id)
            if state is not None:
                point = (state.lon, state.lat)
        if point is None:
            return None
        best: str | None = None
        best_distance = max_m
        for candidate in candidates:
            other = self.snapshot.nodes.get(candidate)
            if other is None:
                continue
            distance = haversine_m(point[0], point[1], other.lon, other.lat)
            if distance < best_distance:
                best, best_distance = candidate, distance
        return best

    def _distance_to_body(self, asset_id: str, body_id: str) -> float:
        key = (asset_id, body_id)
        cached = self._distance_cache.get(key)
        if cached is not None:
            return cached
        node = self.snapshot.nodes.get(asset_id)
        body_node = self.snapshot.nodes.get(body_id)
        if node is None or body_node is None:
            return float("inf")
        distance = haversine_m(node.lon, node.lat, body_node.lon, body_node.lat)
        self._distance_cache[key] = distance
        return distance

    # ------------------------------------------------------------------ #
    # boundary conditions
    # ------------------------------------------------------------------ #
    def tide_m(self, minutes_from_start: float, *, surge_m: float = 0.0) -> float:
        hours = minutes_from_start / 60.0
        phase = 2.0 * math.pi * ((hours + self.tide_phase_h) % TIDE_PERIOD_H) / TIDE_PERIOD_H
        return max(0.05, self.tide_mean_m + self.tide_amplitude_m * math.sin(phase) + surge_m)

    # ------------------------------------------------------------------ #
    # stepping
    # ------------------------------------------------------------------ #
    def step(
        self,
        dt_minutes: float,
        forcing: Forcing,
        *,
        minute_from_start: float = 0.0,
        overrides: Mapping[str, float] | None = None,
    ) -> tuple[dict[str, float], HydroDiagnostics]:
        """Advance the hydraulic state one timestep.

        Returns per-asset street depths (metres) and rich diagnostics so that the
        UI can explain *why* a road is under water.
        """
        dt_s = dt_minutes * 60.0
        overrides = overrides or {}
        nodes = self.snapshot.nodes
        diagnostics = HydroDiagnostics(rainfall_mm_h=forcing.rainfall_mm_h)

        # ---- 1. downstream boundary: the backwater follows the tide --------
        tide_stage = self.tide_m(minute_from_start, surge_m=forcing.surge_m)
        diagnostics.tide_m = round(tide_stage, 3)
        for body in self.water_bodies.values():
            if body.kind != "backwater":
                continue
            target = max(body.invert_elevation_m + 0.15, tide_stage)
            body.stage_m += (target - body.stage_m) * min(1.0, dt_minutes / 40.0)
            body.storage_m3 = body.storage_for_stage(body.stage_m)
            body.gravity_m3s = self._backwater_export(body)
            body.overflow_m3s = 0.0
            body.observations = [f"tidal boundary {tide_stage:.2f} m AMSL (M2 + surge {forcing.surge_m:.2f} m)"]

        # ---- 2. upstream water bodies, in hydraulic order ------------------
        for body_id in self._order:
            body = self.water_bodies[body_id]
            if body.kind == "backwater":
                continue
            node = nodes.get(body.asset_id)
            rainfall = forcing.rainfall_by_zone.get(node.zone or "", forcing.rainfall_mm_h) if node else forcing.rainfall_mm_h
            runoff_m3s = rainfall * body.catchment_km2 * 1000.0 / SECONDS_PER_HOUR * (1.0 - RAINWATER_LOSS_COEFFICIENT)

            upstream = 0.0
            upstream_notes: list[str] = []
            for edge in self.snapshot.in_edges(body.asset_id, kinds=[RelationKind.FEEDS]):
                source = self.water_bodies.get(edge.source_id)
                if source is None or source is body:
                    continue
                passed = min(source.inflow_m3s, source.outflow_m3s + source.inflow_m3s * 0.15)
                # a channel only passes water downstream while a hydraulic gradient
                # exists; once the receiving body is at or above this one, transfer
                # chokes off (this is what makes backwater flooding *persist*)
                if body.stage_m - source.stage_m < 0.02:
                    passed *= 0.25
                if source.kind == "canal" and body.stage_m > body.bankfull_level_m + 0.25:
                    passed *= 0.4
                upstream += passed * float(edge.attenuation)
                if passed > 1.0:
                    upstream_notes.append(f"{source.name} passes {passed:.1f} m³/s")
            released = forcing.release_m3s.get(body.asset_id, 0.0)
            inflow = runoff_m3s + upstream + released
            body.inflow_m3s = inflow
            body.upstream_m3s = upstream

            pumped = 0.0
            for node_state in nodes.values():
                if node_state.asset_type is not AssetType.PUMP_STATION:
                    continue
                for edge in self.snapshot.out_edges(node_state.id, kinds=[RelationKind.DRAINS]):
                    if edge.target_id != body.asset_id:
                        continue
                    rated = float(
                        edge.attributes.get("capacity_m3s") or node_state.capacity.get("pump_rate_m3s") or 0.0
                    )
                    pumped += rated * self._pump_availability(node_state, overrides) * forcing.pump_multiplier.get(node_state.id, 1.0)
            if body.blocked or body.asset_id in forcing.blocked_bodies:
                pumped *= 0.15
            body.pumped_now_m3s = pumped

            gravity = 0.0
            outfall_stage = 0.0
            if body.outfall_asset_id and body.outfall_asset_id in self.water_bodies:
                outfall = self.water_bodies[body.outfall_asset_id]
                outfall_stage = outfall.stage_m
                head = body.stage_m - outfall_stage
                if head > 0.02:
                    gravity = body.gravity_capacity_m3s * min(1.0, math.sqrt(head / 0.6))
                else:
                    gravity = 0.0  # TIDE-LOCKED: the Kochi failure mode
            elif body.kind == "backwater":
                gravity = self._backwater_export(body)
            else:
                gravity = body.gravity_capacity_m3s * 0.6  # unknown outfall: partial discharge
            body.gravity_m3s = gravity

            overflow = WEIR_COEFFICIENT * body.channel_width_m * max(0.0, body.above_bankfull_m) ** 1.5
            body.overflow_m3s = overflow

            delta = (inflow - body.outflow_m3s) * dt_s
            body.storage_m3 = max(0.0, body.storage_m3 + delta)
            body.stage_m = body.stage_for_storage(body.storage_m3)
            body.observations = [
                f"rainfall {rainfall:.1f} mm/h over {body.catchment_km2:.1f} km² → runoff {runoff_m3s:.1f} m³/s",
                f"upstream {upstream:.1f} m³/s" + (f" ({'; '.join(upstream_notes)})" if upstream_notes else ""),
                f"release {released:.1f} m³/s" if released else "no upstream release",
                f"gravity {gravity:.1f} m³/s vs outfall {outfall_stage:.2f} m"
                + (" → TIDE-LOCKED" if gravity <= 0.01 else ""),
                f"pumped {pumped:.1f} m³/s",
                f"overtopping {overflow:.1f} m³/s (stage {body.stage_m:.2f} m, bankfull {body.bankfull_level_m:.2f} m)",
            ]
            diagnostics.total_pumped_m3s += pumped
            diagnostics.total_gravity_m3s += gravity
            if gravity <= 0.01:
                diagnostics.tide_locked_bodies.append(body.name)
            if overflow > 0.5:
                diagnostics.overtopping_bodies.append(body.name)

        # ---- 3. street ponding per zone ------------------------------------
        for zone in self.zones.values():
            rainfall = forcing.rainfall_by_zone.get(zone.zone_id, forcing.rainfall_mm_h)
            zone.rainfall_mm_h = rainfall
            rain_in = rainfall * zone.area_km2 * 1000.0 / SECONDS_PER_HOUR * (1.0 - RAINWATER_LOSS_COEFFICIENT)
            overland = 0.0
            for body_id, weight in self.zone_overland_sources.get(zone.zone_id, []):
                body = self.water_bodies.get(body_id)
                if body is not None:
                    overland += body.overflow_m3s * weight
            zone.overland_in_m3s = overland
            zone.inflow_m3s = rain_in + overland

            capacity = zone.drainage_capacity_m3s * forcing.drainage_multiplier.get(zone.zone_id, 1.0)
            outfall = self.water_bodies.get(zone.outfall_asset_id or "")
            if outfall is not None:
                freeboard = outfall.bankfull_level_m - outfall.stage_m
                # sewers discharge only while the receiving water is below bankfull
                capacity *= max(0.05, min(1.0, 0.30 + freeboard / 1.2))
                if outfall.blocked or outfall.asset_id in forcing.blocked_bodies:
                    capacity *= 0.2
            net = zone.inflow_m3s - capacity
            # runoff concentrates into the low pockets of the ward; the
            # concentration factor is the fraction of ward area that actually
            # ponds, and it is the difference between a plausible model and a
            # model that claims a 60 mm/h storm produces 2 cm of water
            area_m2 = zone.area_km2 * 1_000_000.0 * PONDING_CONCENTRATION
            zone.ponding_m = max(0.0, zone.ponding_m + net * dt_s / area_m2)
            # drainage/recession floor (surface flow to the nearest low point)
            zone.ponding_m = max(0.0, zone.ponding_m - 0.0009 * dt_minutes)
            zone.drained_m3 += capacity * dt_s

        # ---- 4. per-asset depths -------------------------------------------
        depths: dict[str, float] = {}
        for asset_id, node in nodes.items():
            ground = self.asset_ground.get(asset_id, 3.0)
            depth = 0.0
            reasons: list[str] = []

            is_water_body = node.asset_type in {AssetType.CANAL, AssetType.RIVER_GAUGE}
            body = None if is_water_body else self.water_bodies.get(self.asset_outfall.get(asset_id) or "")
            if body is not None:
                distance = self._distance_to_body(asset_id, body.asset_id)
                influence = 900.0 if body.kind == "canal" else (2_200.0 if body.kind == "river" else 1_600.0)
                if body.stage_m > ground + 0.03 and distance <= influence:
                    falloff = max(0.30, 1.0 - distance / influence)
                    candidate = (body.stage_m - ground) * falloff
                    if candidate > depth:
                        depth, _ = candidate, reasons.append(f"{body.name} stage {body.stage_m:.2f} m over ground {ground:.2f} m")

            zone = self.zones.get(node.zone or "")
            if zone is not None:
                elevation_advantage = max(0.0, ground - zone.mean_elevation_m)
                attenuation = max(0.0, 1.0 - elevation_advantage / 1.6)
                candidate = zone.ponding_m * attenuation * 1.35
                if candidate > depth:
                    depth = candidate
                    reasons.append(f"local ponding {zone.ponding_m:.2f} m (rain {zone.rainfall_mm_h:.0f} mm/h, overland {zone.overland_in_m3s:.1f} m³/s)")

            if asset_id in forcing.injections:
                candidate = forcing.injections[asset_id]
                if candidate > depth:
                    depth = candidate
                    reasons.append("scenario injection")

            if is_water_body:
                # a water body reports its own stage relative to bankfull; it is
                # never "flooded" by itself, which was a modelling artefact that
                # made 230 000 people appear to be underwater in light rain
                own = self.water_bodies.get(asset_id)
                depth = own.above_bankfull_m if own is not None else 0.0
                if own is not None and own.above_bankfull_m > 0:
                    reasons.append(
                        f"stage {own.stage_m:.2f} m vs bankfull {own.bankfull_level_m:.2f} m ({own.above_bankfull_m:.2f} m over)"
                    )

            if asset_id in overrides:
                depth = max(depth, float(overrides[asset_id]))

            depths[asset_id] = round(min(depth, MAX_DEPTH_M), 3)

        # ---- 5. point-hazard falloff --------------------------------------
        for asset_id, injected in forcing.injections.items():
            source = nodes.get(asset_id)
            if source is None or injected <= 0:
                continue
            for other_id, other in nodes.items():
                if other_id == asset_id:
                    continue
                distance = haversine_m(source.lon, source.lat, other.lon, other.lat)
                if distance > 900:
                    continue
                candidate = injected * max(0.0, 1.0 - distance / 900.0)
                if candidate > depths.get(other_id, 0.0):
                    depths[other_id] = round(min(candidate, MAX_DEPTH_M), 3)

        if diagnostics.tide_locked_bodies:
            diagnostics.notes.append(
                f"{len(diagnostics.tide_locked_bodies)} water bodies are tide-locked: gravity drainage is inoperative, "
                "all relief depends on pumping"
            )
        if diagnostics.overtopping_bodies:
            diagnostics.notes.append(f"{len(diagnostics.overtopping_bodies)} water bodies are over bankfull and spilling onto streets")
        return depths, diagnostics

    # ------------------------------------------------------------------ #
    def _pump_availability(self, node: AssetState, overrides: Mapping[str, float]) -> float:
        """A pump is worth exactly as much as its power supply and its intake."""
        depth = overrides.get(node.id, node.flood_depth_m)
        if node.status is AssetStatus.FAILED:
            return 0.0
        if depth > 0.5:
            return 0.15
        if not node.power_available:
            hours = float(node.capacity.get("generator_hours") or 0.0)
            if hours <= 0:
                return 0.0
            return 0.75  # generator-backed, but switchover loses throughput
        if node.status is AssetStatus.IMPAIRED:
            return 0.4
        if node.status is AssetStatus.DEGRADED:
            return 0.7
        return 1.0

    @staticmethod
    def _backwater_export(body: WaterBody) -> float:
        head = max(0.0, body.stage_m - body.invert_elevation_m)
        return min(320.0, 70.0 * math.sqrt(max(head, 0.05)))

    # ------------------------------------------------------------------ #
    def apply_intervention(self, intervention_type: InterventionType, payload: Mapping[str, Any]) -> str:
        """Apply an intervention to the hydraulic state, returning an operator-readable note."""
        body_id = payload.get("asset_id")
        body = self.water_bodies.get(str(body_id)) if body_id else None
        if intervention_type is InterventionType.DEPLOY_PUMP:
            capacity = float(payload.get("capacity_m3s") or 0.5)
            quantity = float(payload.get("quantity") or 1.0)
            rated = capacity * quantity
            zone_id = payload.get("zone_id") or self._zone_of(payload.get("destination_id")) or self._zone_of(body_id)
            zone = self.zones.get(str(zone_id)) if zone_id else None
            if zone is None and body is None:
                return "pump deployed, but neither a ward nor a receiving water body could be identified"
            boundary = ""
            if zone is not None:
                # A mobile pump lifts water off the street and discharges it into the
                # canal. Both legs of that move are modelled - lifting the water is
                # what protects the ward, and it is why a pump placed in a canal with
                # no ward association used to appear to do nothing at all.
                zone.drainage_capacity_m3s += rated
                boundary = f" (lifting from {zone.name})"
                if body is None:
                    body = self.water_bodies.get(zone.outfall_asset_id or "")
            if body is not None:
                body.pumped_capacity_m3s += rated * 0.5
                boundary += f" into {body.name}"
            return f"{quantity:.0f} × {capacity:.2f} m³/s mobile pumping{boundary}"
        if intervention_type is InterventionType.SANDBAG_LINE and body is not None:
            body.gravity_capacity_m3s *= 0.85
            return f"sandbag line at {body.name}: inflow is limited, gravity release is slightly reduced"
        if intervention_type is InterventionType.HOLD_WATER_RELEASE:
            return "upstream reservoir release held: downstream discharge capped"
        if intervention_type in {InterventionType.CLOSE_ROAD, InterventionType.REOPEN_ROAD}:
            return f"mobility intervention applied to {payload.get('road_id') or body_id}"
        return "no hydraulic effect for this intervention type"

    def _zone_of(self, asset_id: Any) -> str | None:
        if not asset_id:
            return None
        node = self.snapshot.nodes.get(str(asset_id))
        return node.zone if node is not None else None

    # ------------------------------------------------------------------ #
    def state_snapshot(self) -> dict[str, Any]:
        return {
            "water_bodies": {
                body.asset_id: {
                    "name": body.name,
                    "kind": body.kind,
                    "stage_m": round(body.stage_m, 3),
                    "bankfull_level_m": body.bankfull_level_m,
                    "freeboard_m": body.freeboard_m,
                    "above_bankfull_m": body.above_bankfull_m,
                    "inflow_m3s": round(body.inflow_m3s, 2),
                    "upstream_m3s": round(body.upstream_m3s, 2),
                    "gravity_m3s": round(body.gravity_m3s, 2),
                    "pumped_m3s": round(body.pumped_now_m3s, 2),
                    "overflow_m3s": round(body.overflow_m3s, 2),
                    "pumped_capacity_m3s": round(body.pumped_capacity_m3s, 2),
                    "tide_locked": body.gravity_m3s <= 0.01,
                    "observations": body.observations,
                }
                for body in self.water_bodies.values()
            },
            "zones": {
                zone.zone_id: {
                    "name": zone.name,
                    "ponding_m": round(zone.ponding_m, 3),
                    "rainfall_mm_h": round(zone.rainfall_mm_h, 1),
                    "inflow_m3s": round(zone.inflow_m3s, 2),
                    "overland_in_m3s": round(zone.overland_in_m3s, 2),
                    "drainage_capacity_m3s": round(zone.drainage_capacity_m3s, 2),
                    "over_capacity": zone.over_capacity,
                }
                for zone in self.zones.values()
            },
        }

    def flooding_summary(self, depths: Mapping[str, float]) -> dict[str, Any]:
        flooded = {k: v for k, v in depths.items() if v > 0.05}
        return {
            "flooded_assets": len(flooded),
            "max_depth_m": round(max(depths.values()) if depths else 0.0, 3),
            "mean_depth_m": round(sum(flooded.values()) / len(flooded), 3) if flooded else 0.0,
            "worst": sorted(flooded.items(), key=lambda kv: kv[1], reverse=True)[:10],
            "tide_locked_bodies": [b.name for b in self.water_bodies.values() if b.kind == "canal" and b.gravity_m3s <= 0.01],
        }
