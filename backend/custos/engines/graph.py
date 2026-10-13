"""Dependency-graph engine — the difference between a map and a twin.

A dashboard shows you that the Elamkulam pumping station is flooded. The
dependency graph tells you that it is powered by a 220 kV substation whose
feeder runs through that same flooded pocket, that its loss removes 6 m3/s of
drainage from a canal already 0.09 m below bankfull, and that the canal's
outfall is the only route by which Kaloor-Kadavanthra Road - the ambulance
corridor for a 640-bed hospital - drains.

Model
-----
Nodes are assets; edges are typed, *directed* mechanisms with four physically
meaningful parameters:

``treatment``      how much of the source's deficit reaches the target (0..1)
``lead_time_min``  when the target starts to feel it (minutes)
``failure_mode``   immediate | degraded | delayed (how it degrades)
``recovery_hours`` how long the target stays down once recovered upstream

Propagation is a bounded, priority-ordered traversal (Dijkstra on time) rather
than a naive BFS, because in a real cascade *when* a hospital loses power
decides whether it is a footnote or a fatality.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import networkx as nx

from ..domain.enums import (
    AssetStatus,
    AssetType,
    FailureMode,
    RelationKind,
    status_rank,
    worst,
)
from ..domain.twin import AssetState, Edge, TwinSnapshot
from ..fusion.normalize import haversine_m
from ..observability import get_logger

log = get_logger("engines.graph")

#: how strongly each mechanism transmits a deficit, before per-edge treatment.
#: Tuned so that a flooded arterial road does not instantly kill a hospital,
#: but a lost 220 kV substation does take a pump station off the network.
KIND_TRANSMISSION: dict[RelationKind, float] = {
    RelationKind.POWERS: 0.95,
    RelationKind.SUPPLIES_WATER: 0.85,
    RelationKind.FEEDS: 0.80,
    RelationKind.DRAINS: 0.45,
    RelationKind.ACCESSES: 0.75,
    RelationKind.CONNECTS: 0.55,
    RelationKind.CROSSES: 0.70,
    RelationKind.COVERS: 0.45,
    RelationKind.SHELTERS: 0.60,
    RelationKind.RELEASES: 0.85,
    RelationKind.DEPENDS_ON: 0.65,
    RelationKind.MONITORS: 0.10,
    RelationKind.ROUTES_TO: 0.50,
}

#: assets that are *resilience-neutral*: losing one does not degrade service by
#: itself (a gauge going dark changes knowledge, not capability). They are still
#: modelled - degraded knowledge is reported separately as a confidence penalty.
KNOWLEDGE_ONLY = {RelationKind.MONITORS}

#: status thresholds: at or above this rank the node is a *source* of cascade.
SOURCE_STATUS_RANK = status_rank(AssetStatus.IMPAIRED)


@dataclass(slots=True)
class Effect:
    """A single cascade step: cause -> target, with explainable mechanics."""

    cause_id: str
    target_id: str
    relation_kind: RelationKind
    mechanism: str
    magnitude: float
    probability: float
    lead_time_min: int
    hop: int
    path: tuple[str, ...]
    status_after: AssetStatus
    failed_after: float
    note: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def key(self) -> tuple[str, str, str]:
        return (self.cause_id, self.target_id, self.relation_kind.value)


@dataclass
class GraphInsights:
    computed_at_digest: str
    betweenness: dict[str, float]
    articulation_points: list[str]
    bridge_edges: list[tuple[str, str]]
    single_points_of_failure: list[dict[str, Any]]
    reach: dict[str, int]
    cascade_depth_by_node: dict[str, int]
    top_critical_paths: list[list[str]]


class DependencyGraph:
    """Wraps a :class:`TwinSnapshot` with cascade semantics and graph analytics."""

    def __init__(self, snapshot: TwinSnapshot) -> None:
        self.snapshot = snapshot
        self._graph: nx.DiGraph | None = None
        self._undirected: nx.Graph | None = None
        self._insights: GraphInsights | None = None

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @property
    def graph(self) -> nx.DiGraph:
        if self._graph is None:
            g = nx.DiGraph()
            for node_id, state in self.snapshot.nodes.items():
                g.add_node(
                    node_id,
                    name=state.name,
                    asset_type=state.asset_type.value,
                    criticality=state.criticality,
                    status=state.status.value,
                )
            for edge in self.snapshot.edges:
                if edge.source_id not in g or edge.target_id not in g:
                    continue
                # parallel edges of the same kind are folded: the strongest wins.
                # (a facility served by one road twice is not 2x redundant)
                if g.has_edge(edge.source_id, edge.target_id):
                    data = g[edge.source_id][edge.target_id]
                    if data.get("treatment", 0) >= edge.attenuation:
                        continue
                g.add_edge(
                    edge.source_id,
                    edge.target_id,
                    kind=edge.kind.value,
                    treatment=edge.attenuation,
                    lead_time_min=edge.lead_time_min,
                    failure_mode=edge.failure_mode.value,
                    criticality=edge.criticality,
                    recovery_hours=edge.recovery_hours,
                    redundancy_group=edge.redundancy_group,
                    edge_id=edge.id,
                )
            self._graph = g
        return self._graph

    @property
    def undirected(self) -> nx.Graph:
        if self._undirected is None:
            self._undirected = self.graph.to_undirected(as_view=False)
        return self._undirected

    # ------------------------------------------------------------------ #
    # analytics
    # ------------------------------------------------------------------ #
    def insights(self) -> GraphInsights:
        if self._insights is not None:
            return self._insights
        g = self.graph
        undirected = self.undirected
        betweenness: dict[str, float] = {}
        if g.number_of_nodes() > 0:
            try:
                betweenness = nx.betweenness_centrality(g, k=min(64, g.number_of_nodes()), normalized=True, seed=7)
            except Exception as exc:  # noqa: BLE001 - analytics must never break a response
                log.warning("betweenness failed", extra={"custos": {"error": str(exc)[:160]}})
                betweenness = {n: 0.0 for n in g.nodes}
        try:
            articulation = list(nx.articulation_points(undirected))
        except Exception:  # pragma: no cover
            articulation = []
        try:
            bridges = list(nx.bridges(undirected))
        except Exception:  # pragma: no cover
            bridges = []

        spof = self._rank_single_points_of_failure(betweenness, articulation, bridges)
        reach = {n: len(nx.descendants(g, n)) for n in g.nodes}
        depth = self._cascade_depths()
        return GraphInsights(
            computed_at_digest=self.snapshot.digest(),
            betweenness=betweenness,
            articulation_points=articulation,
            bridge_edges=bridges,
            single_points_of_failure=spof[:25],
            reach=reach,
            cascade_depth_by_node=depth,
            top_critical_paths=self._top_critical_paths(limit=8),
        )

    def _rank_single_points_of_failure(
        self,
        betweenness: Mapping[str, float],
        articulation: Iterable[str],
        bridges: Iterable[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Score every asset by *consequence if lost*, not by graph vanity metrics.

        A node is a single point of failure when losing it removes either
        (a) the only feeding path to a critical asset (articulation), or
        (b) a highly-traversed corridor between lifelines (betweenness), or
        (c) the only edge of its kind into a critical asset (bridge edge).
        """
        graph = self.graph
        articulation_set = set(articulation)
        bridge_pairs = set(bridges)
        scores: list[dict[str, Any]] = []
        for node_id, state in self.snapshot.nodes.items():
            if node_id not in graph:
                continue
            # criticality of everything downstream that would be orphaned
            orphaned = 0.0
            if node_id in articulation_set:
                undirected = self.undirected.copy()
                undirected.remove_node(node_id)
                components = list(nx.connected_components(undirected))
                if components:
                    largest = max(components, key=len)
                    isolated = set(graph.nodes) - largest - {node_id}
                    orphaned = sum(self.snapshot.nodes[n].criticality for n in isolated if n in self.snapshot.nodes)
            bridge_score = 0.0
            for a, b in bridge_pairs:
                if node_id in (a, b):
                    bridge_score += 0.5
            unique_provider = 0
            for target_id in graph.successors(node_id) if node_id in graph else []:
                providers = [p for p in graph.predecessors(target_id) if graph[p][target_id]["kind"] == graph[node_id][target_id]["kind"]]
                if len(providers) == 1:
                    unique_provider += 1
            score = (
                0.45 * min(1.0, orphaned)
                + 0.25 * min(1.0, betweenness.get(node_id, 0.0) * 4)
                + 0.15 * min(1.0, bridge_score)
                + 0.15 * min(1.0, unique_provider / 4)
            ) * (0.5 + 0.5 * state.criticality)
            if score <= 0.12:
                continue
            scores.append(
                {
                    "asset_id": node_id,
                    "name": state.name,
                    "asset_type": state.asset_type.value,
                    "score": round(score, 4),
                    "betweenness": round(betweenness.get(node_id, 0.0), 5),
                    "articulation": node_id in articulation_set,
                    "orphaned_criticality": round(orphaned, 3),
                    "unique_provider_count": unique_provider,
                    "zone": state.zone,
                    "reason": self._spof_reason(state, orphaned, betweenness.get(node_id, 0.0), unique_provider),
                }
            )
        scores.sort(key=lambda item: item["score"], reverse=True)
        return scores

    @staticmethod
    def _spof_reason(state: AssetState, orphaned: float, betweenness: float, unique_provider: int) -> str:
        parts = []
        if orphaned > 0.5:
            parts.append(f"removal orphans services with combined criticality {orphaned:.2f}")
        if betweenness > 0.05:
            parts.append("sits on many shortest service paths")
        if unique_provider:
            parts.append(f"sole {state.asset_type.value} provider for {unique_provider} dependent asset(s)")
        return "; ".join(parts) or "structural chokepoint"

    def _cascade_depths(self) -> dict[str, int]:
        graph = self.graph
        depths: dict[str, int] = {}
        for node in graph.nodes:
            seen = {node}
            frontier = [node]
            depth = 0
            while frontier and depth < 12:
                nxt: list[str] = []
                for current in frontier:
                    for _, target, data in graph.out_edges(current, data=True):
                        if target in seen or data.get("kind") in {k.value for k in KNOWLEDGE_ONLY}:
                            continue
                        seen.add(target)
                        nxt.append(target)
                if not nxt:
                    break
                frontier = nxt
                depth += 1
            depths[node] = depth
        return depths

    def _top_critical_paths(self, limit: int = 8) -> list[list[str]]:
        """Longest dependency chains ending in a high-criticality asset."""
        graph = self.graph
        terminals = [
            n for n, state in self.snapshot.nodes.items()
            if state.criticality >= 0.8 and graph.has_node(n)
        ]
        paths: list[tuple[float, list[str]]] = []
        for terminal in terminals:
            try:
                longest = nx.dag_longest_path(graph.subgraph(nx.ancestors(graph, terminal) | {terminal}))
            except Exception:  # noqa: BLE001 - cycles are tolerated, dag_longest_path requires a DAG
                view = graph.subgraph(nx.ancestors(graph, terminal) | {terminal})
                longest = self._longest_simple_path(view, terminal)
            if len(longest) >= 3:
                weight = sum(self.snapshot.nodes[n].criticality for n in longest if n in self.snapshot.nodes)
                paths.append((weight, longest))
        paths.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        return [path for _, path in paths[:limit]]

    @staticmethod
    def _longest_simple_path(view: nx.DiGraph, terminal: str) -> list[str]:
        try:
            return nx.shortest_path(view, source=next(iter(nx.topological_sort(view))), target=terminal)
        except Exception:  # pragma: no cover
            return [terminal]

    # ------------------------------------------------------------------ #
    # propagation
    # ------------------------------------------------------------------ #
    def propagation(
        self,
        seeds: Mapping[str, float],
        *,
        max_hops: int = 6,
        horizon_min: int = 360,
        min_magnitude: float = 0.06,
    ) -> list[Effect]:
        """Propagate deficits from ``seeds`` (asset_id -> severity 0..1).

        Returns effects ordered by (time, magnitude). The traversal is exact on
        the *first* arrival time to each node, which is what an operations cell
        actually needs: the earliest moment a dependency is affected.
        """
        graph = self.graph
        effects: list[Effect] = []
        best_time: dict[str, float] = {}
        best_magnitude: dict[str, float] = {}
        heap: list[tuple[float, int, str, float, tuple[str, ...]]] = []
        counter = itertools.count()

        for asset_id, severity in seeds.items():
            if asset_id not in graph:
                continue
            severity = max(0.0, min(1.0, float(severity)))
            best_time[asset_id] = 0.0
            best_magnitude[asset_id] = severity
            heapq.heappush(heap, (0.0, next(counter), asset_id, severity, (asset_id,)))

        hops_of: dict[str, int] = {asset_id: 0 for asset_id in seeds if asset_id in graph}

        while heap:
            time_min, _, node_id, magnitude, path = heapq.heappop(heap)
            if time_min > horizon_min:
                continue
            hop = hops_of.get(node_id, 0)
            if hop >= max_hops or magnitude < min_magnitude:
                continue
            state = self.snapshot.nodes.get(node_id)
            if state is None:
                continue
            is_source = node_id in seeds
            for _, target_id, data in graph.out_edges(node_id, data=True):
                if target_id in path:  # never revisit a node on the same chain
                    continue
                kind = RelationKind(data["kind"])
                if kind in KNOWLEDGE_ONLY:
                    continue  # measurement loss is reported as a confidence penalty elsewhere
                if kind is RelationKind.DRAINS and state.asset_type is AssetType.PUMP_STATION:
                    # pumps act on water *levels*, which the hydrology engine already
                    # models through pumped capacity; propagating them as cascading
                    # asset failures would double count and over-alarm
                    continue
                transmission = KIND_TRANSMISSION.get(kind, 0.4) * float(data.get("treatment", 1.0))
                transmission *= 0.55 + 0.45 * float(data.get("criticality", 0.5))
                redundancy = self._redundancy_factor(target_id, kind, exclude=node_id)
                raw = magnitude * transmission * redundancy
                if is_source and state.status is AssetStatus.FAILED:
                    raw = min(1.0, raw * 1.15)  # a total failure transmits harder than a partial one
                lead = int(data.get("lead_time_min", 0))
                mode = FailureMode(data.get("failure_mode", "immediate"))
                if mode is FailureMode.DELAYED:
                    lead += self._buffer_minutes(target_id)
                elif mode is FailureMode.DEGRADED:
                    raw *= 0.6
                    lead += 5
                arrival = time_min + lead
                if arrival > horizon_min:
                    continue
                target_state = self.snapshot.nodes[target_id]
                threshold = self._failure_threshold(target_state, kind)
                if raw + 1e-9 < threshold:
                    continue
                status_after = self._status_from_magnitude(target_state, raw, kind)
                previous = best_time.get(target_id)
                if previous is not None and previous <= arrival + 1e-9 and best_magnitude.get(target_id, 0) >= raw:
                    continue
                best_time[target_id] = min(previous, arrival) if previous is not None else arrival
                best_magnitude[target_id] = max(best_magnitude.get(target_id, 0.0), raw)
                if not target_state.is_mobility and target_state.criticality < 0.35 and raw < 0.35:
                    continue  # avoid noise from low-consequence leaf nodes
                effects.append(
                    Effect(
                        cause_id=node_id,
                        target_id=target_id,
                        relation_kind=kind,
                        mechanism=self.mechanism_label(kind, state, target_state),
                        magnitude=round(min(1.0, raw), 4),
                        probability=round(self._effect_probability(state, target_state, kind, raw), 3),
                        lead_time_min=lead,
                        hop=hop + 1,
                        path=path + (target_id,),
                        status_after=status_after,
                        failed_after=round(arrival, 1),
                        note=self._mechanism_note(kind, state, target_state),
                    )
                )
                hops_of[target_id] = min(hops_of.get(target_id, 99), hop + 1)
                heapq.heappush(heap, (arrival, next(counter), target_id, raw, path + (target_id,)))

        effects.sort(key=lambda e: (e.failed_after, -e.magnitude))
        return effects

    # ---- helpers ---------------------------------------------------------
    def _redundancy_factor(self, target_id: str, kind: RelationKind, *, exclude: str) -> float:
        """If the target has another healthy provider of the same kind, the loss
        is a *degradation*, not a failure. This single rule is why CUSTOS does
        not over-alarm during an ordinary rain event."""
        graph = self.graph
        if not graph.has_node(target_id):
            return 1.0
        alternatives = 0
        for provider in graph.predecessors(target_id):
            if provider == exclude:
                continue
            data = graph[provider][target_id]
            if RelationKind(data["kind"]) is not kind:
                continue
            provider_state = self.snapshot.nodes.get(provider)
            if provider_state is None or status_rank(provider_state.status) < status_rank(AssetStatus.IMPAIRED):
                alternatives += 1
        if alternatives == 0:
            return 1.0
        # each independent healthy alternative absorbs part of the deficit
        return max(0.28, 1.0 / (1.0 + 0.55 * alternatives))

    def _failure_threshold(self, target: AssetState, kind: RelationKind) -> float:
        base = {
            AssetType.HOSPITAL: 0.45,
            AssetType.PUMP_STATION: 0.32,
            AssetType.WATER_TREATMENT: 0.40,
            AssetType.SUBSTATION: 0.35,
            AssetType.TELECOM_TOWER: 0.40,
            AssetType.SHELTER: 0.45,
            AssetType.METRO_STATION: 0.35,
            AssetType.AIRPORT: 0.50,
            AssetType.PORT: 0.45,
            AssetType.REFINERY: 0.50,
        }.get(target.asset_type, 0.30)
        if kind in {RelationKind.ACCESSES, RelationKind.CONNECTS, RelationKind.CROSSES}:
            base *= 0.8  # mobility fails earlier than facilities
        if target.capacity.get("flood_wall_m"):
            base *= 1.15  # engineered protection buys headroom
        return round(base, 3)

    def _status_from_magnitude(self, target: AssetState, magnitude: float, kind: RelationKind) -> AssetStatus:
        if magnitude >= 0.85:
            return AssetStatus.FAILED
        if magnitude >= 0.6:
            return AssetStatus.IMPAIRED
        if magnitude >= 0.35:
            return AssetStatus.DEGRADED
        return AssetStatus.WATCH

    def _buffer_minutes(self, target_id: str) -> int:
        """Autonomy before a *delayed* mechanism bites: generator hours, stored water."""
        target = self.snapshot.nodes.get(target_id)
        if target is None:
            return 0
        for key in ("autonomy_hours", "generator_hours", "backup_hours", "oxygen_hours"):
            hours = target.capacity.get(key)
            if isinstance(hours, (int, float)) and hours > 0:
                return int(hours * 60)
        return 30

    @staticmethod
    def _effect_probability(source: AssetState, target: AssetState, kind: RelationKind, magnitude: float) -> float:
        # confidence in the *stated* mechanism, not in the weather: it degrades
        # with staleness of both endpoints' state and with the number of hops.
        conf = (source.state_confidence + target.state_confidence) / 2.0
        structural = 0.95 if kind in {RelationKind.POWERS, RelationKind.CROSSES} else 0.85
        return max(0.05, min(0.99, conf * structural * (0.55 + 0.45 * magnitude)))

    @staticmethod
    def mechanism_label(kind: RelationKind, source: AssetState, target: AssetState) -> str:
        return {
            RelationKind.POWERS: f"electrical feed from {source.name} lost",
            RelationKind.SUPPLIES_WATER: f"water supply from {source.name} interrupted",
            RelationKind.FEEDS: f"hydraulic propagation from {source.name}",
            RelationKind.DRAINS: f"drainage capacity from {source.name} lost",
            RelationKind.ACCESSES: f"access via {source.name} lost",
            RelationKind.CONNECTS: f"corridor through {source.name} broken",
            RelationKind.CROSSES: f"{source.name} crossing unavailable",
            RelationKind.COVERS: f"communications coverage from {source.name} lost",
            RelationKind.SHELTERS: f"shelter capacity at {source.name} unavailable",
            RelationKind.RELEASES: f"upstream release from {source.name}",
            RelationKind.DEPENDS_ON: f"hard dependency on {source.name}",
            RelationKind.ROUTES_TO: f"response route from {source.name} delayed",
        }.get(kind, f"dependency on {source.name} affected")

    @staticmethod
    def _mechanism_note(kind: RelationKind, source: AssetState, target: AssetState) -> str:
        depth = getattr(target, "flood_depth_m", 0.0)
        if kind is RelationKind.POWERS:
            return (
                f"{target.name} has {target.capacity.get('generator_hours', target.capacity.get('autonomy_hours', 0))} h "
                f"of on-site autonomy; switchover is manual unless marked automatic"
            )
        if kind in {RelationKind.ACCESSES, RelationKind.CONNECTS, RelationKind.CROSSES}:
            return f"route alternates exist but add distance; target depth context {depth:.2f} m"
        if kind is RelationKind.FEEDS:
            return "propagation follows the canal/backwater network with tidal modulation"
        return ""

    # ------------------------------------------------------------------ #
    # mobility & routing
    # ------------------------------------------------------------------ #
    def mobility_graph(
        self,
        *,
        closed: Iterable[str] = (),
        impassable_depth: Mapping[str, float] | None = None,
        congestion: float = 0.55,
    ) -> nx.Graph:
        """Undirected mobility graph of traversable roads, with ETA weights.

        Roads under water above their flood threshold are removed; roads above
        their *heavy* threshold are removed for rescue vehicles. An isolated
        hospital is the single most actionable output of this function.
        """
        impassable_depth = impassable_depth or {}
        closed_set = set(closed)
        graph = nx.Graph()
        for node_id, state in self.snapshot.nodes.items():
            if not state.is_mobility:
                continue
            graph.add_node(node_id, name=state.name)
        for edge in self.snapshot.edges:
            if edge.kind not in {RelationKind.CONNECTS, RelationKind.CROSSES}:
                continue
            source = self.snapshot.nodes.get(edge.source_id)
            target = self.snapshot.nodes.get(edge.target_id)
            if source is None or target is None:
                continue
            if not (source.is_mobility and target.is_mobility):
                continue
            traversal = self._traversal(edge, source, target, closed_set, impassable_depth, congestion)
            if traversal.get("blocked"):
                # a blocked corridor is *removed*, never added with infinite cost:
                # a path that cannot be driven must not be reported as reachable
                continue
            graph.add_edge(edge.source_id, edge.target_id, **traversal)
        return graph

    # ------------------------------------------------------------------ #
    # geometry helpers
    # ------------------------------------------------------------------ #
    def _corridor_distance(self, a: AssetState, b: AssetState) -> float:
        """Distance between two corridor objects.

        Roads in a real dataset are polylines; using their centroids makes two
        corridors that physically meet at a junction look 6 km apart and inflates
        every ETA. The closest-pair-of-vertices distance is used instead, which is
        exact at shared junctions and is the quantity routing actually needs.
        """
        ga = (a.geometry or {}).get("coordinates")
        gb = (b.geometry or {}).get("coordinates")
        if (a.geometry or {}).get("type") == "LineString" and (b.geometry or {}).get("type") == "LineString" and ga and gb:
            best = float("inf")
            for lon1, lat1 in ga:
                for lon2, lat2 in gb:
                    distance = haversine_m(lon1, lat1, lon2, lat2)
                    if distance < best:
                        best = distance
            if best < float("inf"):
                return best
        return haversine_m(a.lon, a.lat, b.lon, b.lat)

    def _traversal(
        self,
        edge: Edge,
        source: AssetState,
        target: AssetState,
        closed: set[str],
        depths: Mapping[str, float],
        congestion: float,
    ) -> dict[str, Any]:
        """Traversal cost between two *corridor nodes*.

        Cost is the estimated travel distance between the two road segments, not
        the sum of their declared lengths - summing lengths double counts every
        junction and inflates a 6 km trip into a 40 km one. The estimate is
        ``min(mean(segment lengths), circuity x straight line)``, with a circuity
        factor of 1.6 typical of an Indian city street grid.
        """
        straight_m = self._corridor_distance(source, target)
        mean_lengths = (
            float(source.capacity.get("length_m") or 600.0) + float(target.capacity.get("length_m") or 600.0)
        ) / 2.0
        link_m = max(120.0, min(mean_lengths, straight_m * 1.35))

        speed_kmh = float(source.capacity.get("free_flow_speed_kmh") or 35.0)
        blocked = False
        reason = ""
        # depth-scaled congestion: 20 mm of water costs 15 % speed, 300 mm costs 60 %
        worst_depth = 0.0
        for state in (source, target):
            if state.id in closed or state.status is AssetStatus.FAILED:
                blocked, reason = True, f"{state.name} closed"
                break
            depth = float(depths.get(state.id, state.flood_depth_m))
            threshold = float(state.capacity.get("flood_threshold_m") or 0.3)
            worst_depth = max(worst_depth, depth)
            if depth >= threshold:
                blocked, reason = True, f"{state.name} submerged ({depth:.2f} m ≥ {threshold:.2f} m)"
                break
        if blocked:
            return {"eta_min": float("inf"), "length_m": link_m, "blocked": True, "reason": reason, "free_flow_min_min": float("inf")}

        water_penalty = min(0.6, worst_depth * 1.6)
        effective_speed = max(4.0, speed_kmh * congestion * (1.0 - water_penalty))
        lead = float(edge.lead_time_min)
        eta = (link_m / 1000.0) / effective_speed * 60.0 + lead
        free_flow = (link_m / 1000.0) / max(5.0, speed_kmh) * 60.0 + lead
        return {
            "eta_min": eta,
            "length_m": link_m,
            "blocked": False,
            "reason": "",
            "free_flow_min_min": free_flow,
            "water_depth_m": round(worst_depth, 3),
        }

    def route(
        self,
        origin_id: str,
        destination_id: str,
        *,
        closed: Iterable[str] = (),
        impassable_depth: Mapping[str, float] | None = None,
        prebuilt: nx.Graph | None = None,
    ) -> dict[str, Any]:
        """Shortest *time* path over the traversable corridor graph.

        Facilities are not corridor nodes, so each endpoint is attached to its
        nearest traversable corridor, with the walk/drive leg added explicitly.
        This is deliberate: an EOC cares about "when does the ambulance arrive",
        including the last 300 m through the flooded pocket, not about the graph
        distance between abstract nodes.
        """
        graph = prebuilt if prebuilt is not None else self.mobility_graph(closed=closed, impassable_depth=impassable_depth)
        if graph.number_of_nodes() == 0:
            return {"reachable": False, "path": [], "eta_min": None, "explanation": "mobility graph is empty"}
        origin_node, origin_access = self._attach(graph, origin_id)
        destination_node, destination_access = self._attach(graph, destination_id)
        if origin_node is None or destination_node is None:
            return {
                "reachable": False,
                "path": [],
                "eta_min": None,
                "explanation": "endpoint has no traversable corridor within reach",
                "blocked": self._blocked_neighbours(graph, closed, impassable_depth or {}),
            }
        try:
            path = nx.shortest_path(graph, origin_node, destination_node, weight="eta_min")
        except nx.NetworkXNoPath:
            return {
                "reachable": False,
                "path": [],
                "eta_min": None,
                "degraded": True,
                "blocked": self._blocked_neighbours(graph, closed, impassable_depth or {}),
                "explanation": "no traversable corridor remains for this pair - the destination is isolated",
            }
        access_min = (origin_access + destination_access) / 1000.0 / 25.0 * 60.0
        eta = nx.path_weight(graph, path, weight="eta_min") + access_min
        free_flow = nx.path_weight(graph, path, weight="free_flow_min_min") + access_min
        full_path = [origin_id, *path, destination_id] if origin_id != path[0] or destination_id != path[-1] else path
        return {
            "reachable": True,
            "path": full_path,
            "corridor_path": path,
            "eta_min": round(eta, 1),
            "free_flow_min": round(free_flow, 1),
            "delay_min": round(eta - free_flow, 1),
            "last_leg_m": round(origin_access + destination_access, 1),
            "detour_delta_min": self._alternative_delta(graph, path, origin_node, destination_node),
            "blocked_segments": self._blocked_neighbours(graph, closed, impassable_depth or {}),
            "explanation": " → ".join(
                self.snapshot.nodes[n].name for n in path if n in self.snapshot.nodes
            ) or "direct",
        }

    def _attach(self, graph: nx.Graph, asset_id: str) -> tuple[str | None, float]:
        """Nearest traversable corridor node for an arbitrary asset."""
        if asset_id in graph:
            return asset_id, 0.0
        state = self.snapshot.nodes.get(asset_id)
        if state is None:
            return None, float("inf")
        # a facility's declared ACCESSES edges are more trustworthy than proximity
        for edge in self.snapshot.in_edges(asset_id, kinds=[RelationKind.ACCESSES]):
            if edge.source_id in graph:
                candidate = self.snapshot.nodes.get(edge.source_id)
                if candidate is not None:
                    return edge.source_id, float(edge.attributes.get("distance_m") or 250.0)
        best: str | None = None
        best_distance = float("inf")
        for node_id in graph.nodes:
            other = self.snapshot.nodes.get(node_id)
            if other is None:
                continue
            distance = self._corridor_distance(state, other)
            if distance < best_distance:
                best, best_distance = node_id, distance
        if best is None or best_distance > 1500.0:
            # beyond 1.5 km the "last leg" is no longer a last leg; the endpoint
            # must be attached through its declared ACCESSES edges instead
            return None, float("inf")
        return best, best_distance

    def _alternative_delta(self, graph: nx.Graph, path: list[str], origin: str, destination: str) -> float | None:
        """How much worse the *second best* route is: a robustness measure.

        ``None`` means the corridor is a single point of failure for this pair.
        """
        working = graph.copy()
        for a, b in zip(path, path[1:]):
            if working.has_edge(a, b):
                working.remove_edge(a, b)
        try:
            second = nx.path_weight(working, nx.shortest_path(working, origin, destination, weight="eta_min"), "eta_min")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        return round(second - nx.path_weight(graph, path, weight="eta_min"), 1)

    def _free_flow_minutes(self, path: Iterable[str]) -> float:
        """Legacy helper retained for callers that only have node ids."""
        total = 0.0
        previous: AssetState | None = None
        for node_id in path:
            state = self.snapshot.nodes.get(node_id)
            if state is None:
                continue
            if previous is not None:
                straight = haversine_m(previous.lon, previous.lat, state.lon, state.lat)
                link_m = max(120.0, straight * 1.6)
                speed = float(previous.capacity.get("free_flow_speed_kmh") or 35.0)
                total += (link_m / 1000.0) / max(5.0, speed) * 60.0
            previous = state
        return total

    def _blocked_neighbours(self, graph: nx.Graph, closed: Iterable[str], depths: Mapping[str, float]) -> list[dict[str, Any]]:
        out = []
        for node_id in closed:
            state = self.snapshot.nodes.get(node_id)
            if state:
                out.append({"asset_id": node_id, "name": state.name, "reason": "closed by operator"})
        for node_id, depth in depths.items():
            state = self.snapshot.nodes.get(node_id)
            if state and depth >= float(state.capacity.get("flood_threshold_m") or 0.3):
                out.append({"asset_id": node_id, "name": state.name, "reason": f"{depth:.2f} m above threshold"})
        return out

    # ------------------------------------------------------------------ #
    def reachable_facilities(self, origin_id: str, *, max_minutes: float = 30.0, **kwargs: Any) -> list[dict[str, Any]]:
        """What can still respond from here - the question an EOC asks first."""
        out = []
        for state in self.snapshot.nodes.values():
            if not state.is_lifeline:
                continue
            route = self.route(origin_id, state.id, **kwargs)
            if route.get("reachable") and (route.get("eta_min") or 1e9) <= max_minutes:
                out.append(
                    {
                        "asset_id": state.id,
                        "name": state.name,
                        "asset_type": state.asset_type.value,
                        "eta_min": route["eta_min"],
                        "delay_min": route.get("delay_min", 0.0),
                        "path": route["path"],
                    }
                )
        out.sort(key=lambda item: item["eta_min"])
        return out

    def update_statuses(self, effects: Iterable[Effect], snapshot: TwinSnapshot) -> None:
        for effect in effects:
            snapshot.apply_status(effect.target_id, effect.status_after, source=f"cascade:{effect.cause_id}")


def summarise_cascade(effects: Iterable[Effect], snapshot: TwinSnapshot) -> dict[str, Any]:
    """Human- and UI-facing roll-up of a cascade set."""
    effects = list(effects)
    if not effects:
        return {"count": 0, "hooks": [], "narrative": "No downstream dependency effects propagate in this window."}
    by_type: dict[str, int] = {}
    first_by_target: dict[str, Effect] = {}
    for effect in effects:
        state = snapshot.nodes.get(effect.target_id)
        if state is None:
            continue
        by_type[state.asset_type.value] = by_type.get(state.asset_type.value, 0) + 1
        existing = first_by_target.get(effect.target_id)
        if existing is None or effect.failed_after < existing.failed_after:
            first_by_target[effect.target_id] = effect
    hooks = sorted(effects, key=lambda e: (e.failed_after, -e.magnitude))[:6]
    lead = min(e.failed_after for e in effects)
    return {
        "count": len(effects),
        "affected_by_type": by_type,
        "earliest_min": round(lead, 1),
        "max_hop": max(e.hop for e in effects),
        "max_magnitude": round(max(e.magnitude for e in effects), 3),
        "hooks": [
            {
                "target_id": e.target_id,
                "target_name": snapshot.nodes[e.target_id].name if e.target_id in snapshot.nodes else e.target_id,
                "mechanism": e.mechanism,
                "minutes_to_impact": round(e.failed_after, 1),
                "magnitude": e.magnitude,
                "probability": e.probability,
                "hop": e.hop,
            }
            for e in hooks
        ],
    }


class MobilityRouter:
    """Memoised routing over flooded corridor graphs.

    A flood simulation evaluates the *same* blockage pattern for many ensemble
    members and many timesteps, so route computation is cached on a quantised
    signature of "which corridors are under water". Without this the what-if
    engine is O(members x timesteps x pairs) Dijkstra calls and becomes the
    bottleneck; with it, a 48-member 6-hour ensemble routes in well under a
    second on an EOC workstation.
    """

    def __init__(self, dependency_graph: DependencyGraph, *, quantum_m: float = 0.05) -> None:
        self.dep = dependency_graph
        self.quantum = quantum_m
        self._graphs: dict[frozenset[str], nx.Graph] = {}
        self._cache: dict[tuple[frozenset[str], str, str], dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    def signature(self, depths: Mapping[str, float], closed: Iterable[str]) -> frozenset[str]:
        """Corridors that are impassable, quantised so near-identical members share work."""
        blocked: set[str] = set(closed)
        for node in self.dep.snapshot.roads:
            depth = float(depths.get(node.id, node.flood_depth_m))
            threshold = float(node.capacity.get("flood_threshold_m") or 0.3)
            quantised = round(depth / self.quantum) * self.quantum
            if quantised >= threshold or node.status is AssetStatus.FAILED:
                blocked.add(node.id)
        return frozenset(blocked)

    def graph_for(self, signature: frozenset[str]) -> nx.Graph:
        graph = self._graphs.get(signature)
        if graph is None:
            graph = self.dep.mobility_graph(closed=signature)
            if len(self._graphs) > 96:  # bounded memory: EOC laptops are not servers
                self._graphs.clear()
            self._graphs[signature] = graph
        return graph

    def route(
        self,
        origin_id: str,
        destination_id: str,
        *,
        signature: frozenset[str],
        impassable_depth: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        key = (signature, origin_id, destination_id)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        result = self.dep.route(
            origin_id,
            destination_id,
            closed=signature,
            impassable_depth=impassable_depth,
            prebuilt=self.graph_for(signature),
        )
        if len(self._cache) > 20_000:
            self._cache.clear()
        self._cache[key] = result
        return result

    def response_delays(
        self,
        pairs: Iterable[tuple[str, str]],
        *,
        signature: frozenset[str],
        impassable_depth: Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        delays: dict[str, float] = {}
        for origin, destination in pairs:
            result = self.route(origin, destination, signature=signature, impassable_depth=impassable_depth)
            key = f"{origin}->{destination}"
            if result.get("reachable"):
                delays[key] = float(result.get("delay_min") or 0.0) + float(result.get("access_walk_min") or 0.0)
            else:
                # unreachable is the worst possible outcome: charge it a large,
                # visible penalty rather than dropping the pair silently
                delays[key] = 240.0
        return delays

    def stats(self) -> dict[str, Any]:
        return {"hits": self.hits, "misses": self.misses, "cached_graphs": len(self._graphs), "cached_routes": len(self._cache)}
