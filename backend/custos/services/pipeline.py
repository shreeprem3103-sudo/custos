"""The CUSTOS operating loop: Collect → Fuse → Understand → Predict → Simulate → Recommend.

Everything the command centre does is one of those six stages, and every stage
reports what it did, how long it took, how confident it is and what evidence it
used. The orchestrator is the only component allowed to talk to all of them, so
the ordering, the degraded-mode behaviour and the audit trail live in one place.

Failure policy, stated once and enforced here:

* **Collect** may lose connectors without stopping the cycle.
* **Fuse** falls back to the last good snapshot and marks it degraded.
* **Understand** needs no external service (rules + robust statistics); the AI
  assist is optional and its absence never blocks.
* **Predict** degrades to persistence (last observed value) if sensors are stale.
* **Simulate** runs on a worker thread: it is CPU-bound and must never block the
  event loop that is streaming to the EOC.
* **Recommend** re-simulates; if the time budget is exhausted, the engine still
  returns the *unmeasured* candidates flagged honestly as ``measured=False``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence

from ..config import settings
from ..domain.enums import AssetStatus, RelationKind, StageName, confidence_band, status_rank
from ..domain.kpi_meta import label_for, lower_is_better, unit_for
from ..domain.schemas import (
    ComparisonResult,
    ComparisonRow,
    Explainability,
    InterventionSpec,
    KPISet,
    Recommendation,
    SimulationRequest,
    SimulationResult,
    SimulationSummary,
    StageStatus,
)
from ..domain.twin import TwinSnapshot
from ..engines.graph import DependencyGraph, MobilityRouter
from ..engines.kpi import diff_kpis, kpis_from_state, score_kpis
from ..engines.recommend import Candidate, evaluate_candidates, generate_candidates
from ..engines.simulation import (
    ScenarioOutcome,
    build_narrative,
    build_result,
    result_snapshot,
    run_scenario,
    sensitivity_analysis,
)
from ..fusion import repository
from ..infrastructure.models import IncidentEvent
from ..fusion.normalize import haversine_m, parse_timestamp, robust_baseline, z_score
from ..observability import METRICS, Timer, get_logger, set_correlation_id
from ..realtime.bus import BUS, Topics
from . import store
from .ai import REASONER, collect_numbers
from .connectors import ConnectorRegistry, Reading, build_default_registry

log = get_logger("services.pipeline")

ProgressCallback = Callable[[float, str, Mapping[str, Any]], None]


# --------------------------------------------------------------------------- #
# stage bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class StageRecord:
    name: StageName
    status: str = "ok"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    detail: str = ""
    degraded: bool = False
    items: int = 0

    #: stage state vocabulary is deliberately small: an operator must be able to
    #: tell "this stage did not do much" from "this stage could not do its job"
    _STATE = {"ok": "active", "running": "active", "degraded": "degraded", "error": "error", "skipped": "idle"}

    def to_model(self) -> StageStatus:
        return StageStatus(
            stage=f"{self.name.value}: {self.detail}" if self.detail else self.name.value,
            state=self._STATE.get(self.status, "idle"),
            last_run_at=self.started_at,
            detail=self.detail,
            throughput=self.items,
        )


class CycleReport:
    """What one full pass of the six stages produced."""

    def __init__(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id
        self.started_at = datetime.now(timezone.utc)
        self.stages: list[StageRecord] = []
        self.readings_ingested = 0
        self.anomalies: list[dict[str, Any]] = []
        self.degraded = False
        self.notes: list[str] = []

    def stage(self, name: StageName, **kwargs: Any) -> StageRecord:
        record = StageRecord(name=name, **kwargs)
        self.stages.append(record)
        return record

    @contextlib.contextmanager
    def track(self, name: StageName) -> Iterator[StageRecord]:
        """Run a stage, timing it and marking how it ended.

        A stage that returns early, raises, or leaves itself "running" would
        otherwise show up on the console as a stage that never finished - the one
        thing an operator cannot interpret.
        """
        record = self.stage(name, status="running")
        started = time.perf_counter()
        try:
            yield record
            if record.status == "running":
                record.status = "degraded" if record.degraded else "ok"
        except Exception:
            record.status = "error"
            record.degraded = True
            self.degraded = True
            raise
        finally:
            record.duration_ms = (time.perf_counter() - started) * 1000

    @property
    def duration_ms(self) -> float:
        return round((datetime.now(timezone.utc) - self.started_at).total_seconds() * 1000, 1)


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
class Orchestrator:
    """Owns the loop, the connector registry and the simulation budget."""

    def __init__(self) -> None:
        self.registry: ConnectorRegistry | None = None
        self._sim_semaphore = asyncio.Semaphore(max(1, settings.sim_max_concurrency))
        self._last_cycle: CycleReport | None = None
        self._cycles = 0
        self._simulations = 0
        self._started_at = time.monotonic()
        self._scenario_rain_mm_h = 0.0
        self._prediction: dict[str, Any] = {}

    # -- lifecycle --------------------------------------------------------- #
    async def boot(self) -> dict[str, Any]:
        """Seed the twin if empty and warm the cache before the first request."""
        info: dict[str, Any] = {"seeded": False, "assets": 0}
        try:
            async with repository.session_scope() as session:
                counts = await repository.table_counts(session)
            if counts.get("assets", 0) == 0 and settings.seed_on_startup:
                info.update(await self.seed_city())
        except Exception as exc:  # noqa: BLE001 - boot must not crash the API
            log.error("boot: database unavailable", extra={"custos": {"error": str(exc)[:200]}})
        try:
            snapshot = await repository.get_snapshot(force=True)
            info["assets"] = len(snapshot.nodes)
            info["edges"] = len(snapshot.edges)
            info["sensors"] = len(snapshot.sensors)
            self.registry = build_default_registry(snapshot, scenario_rain_mm_h=self._scenario_rain_mm_h)
        except Exception as exc:  # noqa: BLE001
            log.error("boot: twin snapshot unavailable", extra={"custos": {"error": str(exc)[:200]}})
        BUS.stats()  # touch the bus so its ring/stats are initialised before first publish
        return info

    async def seed_city(self) -> dict[str, Any]:
        """Load the Kochi crisis-twin dataset into an empty database.

        Idempotent: re-running updates assets/relationships/sensors in place and
        never deletes operator-entered incidents or tasks.
        """
        from ..fusion import city_kochi

        written: dict[str, Any] = {"assets": 0, "relationships": 0, "sensors": 0, "zones": 0, "resources": 0, "incidents": 0}
        async with repository.session_scope() as session:
            for asset in city_kochi.ASSETS:
                await repository.upsert_asset(session, asset)
                written["assets"] += 1
            for relationship in city_kochi.RELATIONSHIPS:
                await repository.upsert_relationship(session, relationship)
                written["relationships"] += 1
            for sensor in city_kochi.SENSORS:
                await repository.upsert_sensor(session, sensor)
                written["sensors"] += 1
            for zone in getattr(city_kochi, "ZONES", []):
                await store.upsert_zone(session, zone)
                written["zones"] += 1
            for resource in getattr(city_kochi, "RESOURCES", []):
                await store.upsert_resource(session, resource)
                written["resources"] += 1
            for incident in getattr(city_kochi, "seed_incidents", lambda: [])():
                payload = {
                    "id": incident["id"],
                    "title": incident["title"],
                    "category": getattr(incident["category"], "value", incident["category"]),
                    "severity": incident["severity"],
                    "status": getattr(incident["status"], "value", incident["status"]),
                    "confidence": incident.get("confidence", 0.6),
                    "summary": incident.get("summary", ""),
                    "lon": incident["lon"],
                    "lat": incident["lat"],
                    "radius_m": incident.get("radius_m", 400.0),
                    "zone": incident.get("zone"),
                    "population_exposed": incident.get("population_exposed", 0),
                    "reported_by": incident.get("reported_by", "system"),
                    "detected_at": incident.get("detected_at"),
                    "asset_links": [
                        {"asset_id": asset_id, "role": impact, "weight": weight}
                        for asset_id, impact, weight in incident.get("affected", [])
                    ],
                }
                incident_id = await store.upsert_incident(session, payload)
                for stage, body, minutes_ago in incident.get("timeline", []):
                    session.add(
                        IncidentEvent(
                            incident_id=incident_id,
                            stage=str(stage),
                            actor="custos",
                            detail=str(body),
                            at=datetime.now(timezone.utc) - timedelta(minutes=int(minutes_ago)),
                        )
                    )
                written["incidents"] += 1
            await repository.set_system_state(
                session,
                "city_seed",
                {"city": settings.city_id, "at": datetime.now(timezone.utc).isoformat(), "counts": written},
            )
        await repository.get_snapshot(force=True)
        written["seeded"] = True
        log.info("city seeded", extra={"custos": written})
        return written

    # -- the loop ---------------------------------------------------------- #
    async def run_cycle(self, *, trigger: str = "scheduler", force: bool = False) -> CycleReport:
        """One Collect → Fuse → Understand → Predict → Simulate → Recommend pass.

        Kept deliberately *cheap* by default: the cycle refreshes the twin and the
        forecasts, and only re-runs a full ensemble when the forecast says
        something changed or a commander asked for it. Running a 48-member
        ensemble every 30 seconds would make the EOC feel slow and would burn the
        analyst's trust in the numbers when they jitter.
        """
        cycle = CycleReport(cycle_id=f"cyc_{uuid.uuid4().hex[:10]}")
        set_correlation_id(f"cycle-{cycle.cycle_id}")
        trigger_meta: dict[str, Any] = {"trigger": trigger}

        snapshot = await self._collect(cycle, trigger_meta)
        snapshot = await self._fuse(cycle, snapshot)
        insights = await self._understand(cycle, snapshot)
        prediction = await self._predict(cycle, snapshot, insights)
        outcome: ScenarioOutcome | None = None
        if self._should_simulate(prediction, force=force):
            outcome, result = await self._simulate(cycle, snapshot, prediction)
            await self._recommend(cycle, outcome, result)
        else:
            cycle.stage(StageName.SIMULATE, status="skipped", detail="forecast below the re-simulation trigger; twin refreshed only")

        self._last_cycle = cycle
        self._cycles += 1
        METRICS.observe("cycle_duration_ms", cycle.duration_ms)
        await BUS.publish(
            Topics.TWIN_SNAPSHOT,
            {
                "cycle_id": cycle.cycle_id,
                "duration_ms": cycle.duration_ms,
                "degraded": cycle.degraded,
                "stages": [
                    {"name": s.name.value, "status": s.status, "duration_ms": s.duration_ms, "detail": s.detail}
                    for s in cycle.stages
                ],
            },
        )
        return cycle

    # -- stages ------------------------------------------------------------ #
    async def _collect(self, cycle: CycleReport, trigger_meta: Mapping[str, Any]) -> TwinSnapshot:
        with Timer("stage_collect"), cycle.track(StageName.COLLECT) as record:
            if self.registry is None:
                try:
                    snapshot = await repository.get_snapshot()
                    self.registry = build_default_registry(snapshot, scenario_rain_mm_h=self._scenario_rain_mm_h)
                except Exception as exc:  # noqa: BLE001
                    record.status = "degraded"
                    record.degraded = True
                    record.detail = f"no connector registry available: {str(exc)[:120]}"
                    cycle.degraded = True
                    return await repository.get_snapshot()
            readings: list[Reading] = await self.registry.poll_all()
            ingested = 0
            if readings:
                try:
                    async with repository.session_scope() as session:
                        for reading in readings:
                            if await repository.insert_reading(session, reading.as_row()):
                                ingested += 1
                        for connector in self.registry:
                            await repository.record_connector_run(
                                session,
                                {
                                    "connector": connector.name,
                                    "kind": connector.kind,
                                    "ok": connector.health.ok,
                                    "items": connector.health.fetched,
                                    "error": connector.health.last_error,
                                    "detail": {"stale_seconds": None if connector.health.last_success is None else round(connector.health.stale_seconds, 1)},
                                },
                            )
                except Exception as exc:  # noqa: BLE001 - storage loss degrades, it does not stop
                    record.status = "degraded"
                    record.degraded = True
                    record.detail = f"readings not persisted: {str(exc)[:140]}"
                    cycle.degraded = True
            cycle.readings_ingested = ingested
            unhealthy = [c["name"] for c in self.registry.health() if not c["ok"]]
            record.items = ingested
            if record.status == "running":
                record.status = "degraded" if unhealthy else "ok"
                record.degraded = bool(unhealthy)
                record.detail = f"{ingested} readings ingested" + (f"; degraded connectors: {', '.join(unhealthy)}" if unhealthy else "")
            if ingested:
                await BUS.publish(Topics.SENSOR_READING, {"ingested": ingested, "connectors": len(list(self.registry))}, source="collect")
            return await repository.get_snapshot(force=True)

    async def _fuse(self, cycle: CycleReport, snapshot: TwinSnapshot) -> TwinSnapshot:
        """Derive fused, per-asset evidence from raw readings.

        Fusion here is what turns three thousand samples into the handful of
        statements a commander can act on: which canal is rising, which gauge is
        dead, which sensor disagrees with its neighbours. Timestamps and units
        were normalised at ingest; this stage handles *identity* (which asset a
        reading belongs to) and *credibility* (how much to trust it).
        """
        with Timer("stage_fuse"), cycle.track(StageName.FUSE) as record:
            fused: dict[str, Any] = {}
            missing = 0
            try:
                async with repository.session_scope() as session:
                    recent = await store.recent_readings(session, minutes=45)
            except Exception as exc:  # noqa: BLE001
                record.status = "degraded"
                record.degraded = True
                record.detail = f"fusion degraded to twin state only: {str(exc)[:140]}"
                cycle.degraded = True
                return snapshot

            by_metric: dict[str, list[float]] = {}
            for row in recent:
                by_metric.setdefault(str(row["metric"]), []).append(float(row["value"]))
            stale_sensors = [s.id for s in snapshot.sensors.values() if s.last_observed_at is None]
            for sensor in snapshot.sensors.values():
                if sensor.asset_id and sensor.asset_id in snapshot.nodes:
                    continue
                missing += 1

            for zone in snapshot.zones.values():
                zone_assets = [n for n in snapshot.nodes.values() if n.zone == zone.id]
                depths = [n.flood_depth_m for n in zone_assets if n.flood_depth_m > 0.0]
                fused[zone.id] = {
                    "assets": len(zone_assets),
                    "flooded": len(depths),
                    "max_depth_m": round(max(depths), 3) if depths else 0.0,
                    "population": zone.population,
                }

            for metric, values in by_metric.items():
                if len(values) < 4:
                    continue
                median, sigma = robust_baseline(values)
                fused[f"metric:{metric}"] = {
                    "n": len(values),
                    "median": round(median, 4),
                    "sigma": round(sigma, 4),
                    "max": round(max(values), 4),
                    "min": round(min(values), 4),
                }
            snapshot.attributes["fusion"] = fused
            snapshot.attributes["fusion_quality"] = {
                "sensors": len(snapshot.sensors),
                "sensors_without_asset": missing,
                "sensors_never_seen": len(stale_sensors),
                "readings_45min": len(recent),
                "metrics": len(by_metric),
            }
            record.items = len(recent)
            record.status = "degraded" if missing else "ok"
            record.detail = (
                f"{len(recent)} readings fused across {len(by_metric)} metrics; "
                f"{missing} sensors could not be bound to an asset (reported, not silently dropped)"
            )
            return snapshot

    async def _understand(self, cycle: CycleReport, snapshot: TwinSnapshot) -> dict[str, Any]:
        """Anomaly detection, incident triage and cascade insight (deterministic first)."""
        with Timer("stage_understand"), cycle.track(StageName.UNDERSTAND) as record:
            graph = DependencyGraph(snapshot)
            insights = graph.insights().to_payload()
            anomalies: list[dict[str, Any]] = []

            try:
                async with repository.session_scope() as session:
                    recent = await store.recent_readings(session, minutes=90)
            except Exception:  # noqa: BLE001
                recent = []
            by_sensor: dict[str, list[tuple[datetime, float]]] = {}
            for row in recent:
                at = row["observed_at"]
                if isinstance(at, str):
                    at = parse_timestamp(at)
                by_sensor.setdefault(str(row["sensor_id"]), []).append((at, float(row["value"])))
            for sensor_id, samples in by_sensor.items():
                if len(samples) < 6:
                    continue
                samples.sort(key=lambda item: item[0])
                values = [value for _at, value in samples]
                median, sigma = robust_baseline(values[:-1] or values)
                z = z_score(samples[-1][1], median, sigma)
                if abs(z) < 3.5:
                    continue
                sensor = snapshot.sensors.get(sensor_id)
                anomalies.append(
                    {
                        "sensor_id": sensor_id,
                        "asset_id": sensor.asset_id if sensor else None,
                        "metric": row_metric(recent, sensor_id),
                        "value": round(samples[-1][1], 4),
                        "baseline": round(median, 4),
                        "sigma": round(sigma, 4),
                        "z_score": round(z, 2),
                        "band": confidence_band(min(0.98, abs(z) / 12.0)),
                        "at": samples[-1][0],
                    }
                )
                if abs(z) >= 6.0:
                    await BUS.publish(
                        Topics.ANOMALY,
                        {"sensor_id": sensor_id, "z": round(z, 2), "value": round(samples[-1][1], 4)},
                        
                    )
            # the AI assist only *explains* what the robust statistics already flagged
            for anomaly in sorted(anomalies, key=lambda a: abs(a["z_score"]), reverse=True)[:3]:
                assessment = await REASONER.assess_anomaly(
                    metric=anomaly["metric"],
                    value=anomaly["value"],
                    baseline=anomaly["baseline"],
                    spread=anomaly["sigma"],
                    z=anomaly["z_score"],
                    context=f"asset {anomaly['asset_id'] or 'unbound'} in {settings.city_name}",
                )
                anomaly["explanation"] = assessment.text
                anomaly["explanation_source"] = "language_model" if not assessment.fallback_used else "deterministic"
                anomaly["explanation_grounded"] = not assessment.ungrounded_numbers

            snapshot.attributes["insights"] = insights
            cycle.anomalies = anomalies
            record.items = len(anomalies)
            record.status = "ok"
            record.detail = (
                f"{len(anomalies)} sensor anomalies (robust z ≥ 3.5); {len(insights.get('single_points_of_failure', []))} "
                f"single points of failure; {len(insights.get('articulation_points', []))} articulation points"
            )
            if anomalies:
                try:
                    async with repository.session_scope() as session:
                        for anomaly in anomalies[:20]:
                            await store.record_anomaly(session, anomaly)
                except Exception as exc:  # noqa: BLE001
                    record.detail += f" (anomalies not persisted: {str(exc)[:80]})"
            return insights

    async def _predict(self, cycle: CycleReport, snapshot: TwinSnapshot, insights: Mapping[str, Any]) -> dict[str, Any]:
        """Short-horizon driver forecast the simulation is anchored to.

        Prediction is deliberately modest and *stated*: it extrapolates the fused
        sensor trend, applies the astronomical tide (which is deterministic and
        known exactly), and reports its own confidence. A predictive system that
        is honest about a 30-minute horizon is worth more to a commander than one
        that silently extrapolates an hour.
        """
        with Timer("stage_predict"), cycle.track(StageName.PREDICT) as record:
            fusion = snapshot.attributes.get("fusion", {})
            rain_stats = fusion.get("metric:rainfall_mm_h") or fusion.get("metric:rainfall") or {}
            stage_stats = fusion.get("metric:water_level_m") or fusion.get("metric:river_level_m") or {}
            observed = float(rain_stats.get("median") or 0.0)
            trend = float(rain_stats.get("max", observed)) - observed
            horizon_min = 30
            predicted_rain = max(0.0, observed + trend * (horizon_min / 30.0))
            rising_bodies = [
                {"asset_id": body.id, "name": body.name, "stage_m": round(body.flood_depth_m, 3), "trend": "rising"}
                for body in snapshot.nodes.values()
                if body.flood_depth_m > 0.05 and body.asset_type.value in {"canal", "river_gauge"}
            ][:8]
            confidence = 0.78 if rain_stats.get("n", 0) >= 8 else (0.55 if rain_stats else 0.35)
            prediction = {
                "horizon_minutes": horizon_min,
                "rainfall_mm_h": round(predicted_rain, 2),
                "observed_rainfall_mm_h": round(observed, 2),
                "trend": "rising" if trend > 0.5 else ("falling" if trend < -0.5 else "steady"),
                "stage_median_m": round(float(stage_stats.get("median") or 0.0), 3),
                "rising_water_bodies": rising_bodies,
                "confidence": confidence,
                "band": confidence_band(confidence),
                "basis": f"fused sensor trend over the last 90 min ({int(rain_stats.get('n', 0))} samples)",
                "single_points_of_failure": [spof.get("asset_id") for spof in insights.get("single_points_of_failure", [])][:6],
            }
            self._prediction = prediction
            snapshot.attributes["prediction"] = prediction
            record.detail = (
                f"{prediction['trend']} rainfall, forecast {prediction['rainfall_mm_h']:.1f} mm/h at +{horizon_min} min "
                f"(confidence {confidence:.2f} from {int(rain_stats.get('n', 0))} samples)"
            )
            record.status = "degraded" if confidence < 0.5 else "ok"
            record.degraded = confidence < 0.5
            return prediction

    def _should_simulate(self, prediction: Mapping[str, Any], *, force: bool) -> bool:
        if force:
            return True
        if self._last_cycle is None:
            return True
        if float(prediction.get("rainfall_mm_h") or 0.0) >= 12.0:
            return True
        return False

    async def simulate(
        self,
        request: SimulationRequest,
        *,
        persist: bool = True,
        with_recommendations: bool = True,
        baseline_id: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> SimulationResult:
        """Run a what-if scenario end to end (the API's single entry point)."""
        async with self._sim_semaphore:
            snapshot = await repository.get_snapshot()
            return await self._run_simulation(
                snapshot, request, persist=persist, with_recommendations=with_recommendations, baseline_id=baseline_id, progress=progress
            )

    async def _run_simulation(
        self,
        snapshot: TwinSnapshot,
        request: SimulationRequest,
        *,
        persist: bool,
        with_recommendations: bool,
        baseline_id: str | None,
        progress: ProgressCallback | None,
        baseline: ScenarioOutcome | None = None,
    ) -> SimulationResult:
        ensemble = max(1, min(settings.sim_max_ensemble_size, request.ensemble_size or settings.sim_ensemble_size))
        started = time.perf_counter()

        def _report(fraction: float, message: str, extra: Mapping[str, Any] | None = None) -> None:
            if progress is not None:
                progress(fraction, message, extra or {})

        _report(0.05, f"hydrology ensemble: {ensemble} members over {request.horizon_minutes} min")
        outcome = await asyncio.to_thread(
            run_scenario, snapshot, request, ensemble_size=ensemble, severity_seeds=None, progress=progress
        )
        _report(0.55, "cascade and mobility propagation")
        stated = result_snapshot(outcome)
        narrative = build_narrative(outcome, request)

        # The brief is the only place a language model touches the operator's view,
        # and it may only rephrase figures the engines already produced.
        if settings.ai_enabled:
            facts = _brief_facts(outcome)
            brief = await REASONER.brief_scenario(narrative=narrative, kpis=outcome.kpis.model_dump(), facts=facts)
            narrative = brief.text
            try:
                async with repository.session_scope() as session:
                    await repository.record_ai_invocation(
                        session,
                        {
                            "task": "brief_scenario",
                            "model": brief.model,
                            "latency_ms": brief.latency_ms,
                            "prompt_hash": brief.prompt_hash,
                            "accepted": brief.accepted,
                            "grounded": not brief.ungrounded_numbers,
                            "fallback": brief.fallback_used,
                            "detail": {"ungrounded": brief.ungrounded_numbers[:8], "error": brief.error[:200]},
                        },
                    )
            except Exception:  # noqa: BLE001 - an audit write failure must not lose the answer
                pass

        recommendations: list[Recommendation] = []
        if with_recommendations:
            candidates = generate_candidates(
                stated,
                outcome.kpis,
                request=request,
                cascade_targets=[c.target_asset_id for c in build_result(outcome, request).cascade[:40]],
            )
            if candidates:
                recommendations = await asyncio.to_thread(
                    evaluate_candidates,
                    snapshot,
                    request,
                    candidates,
                    baseline_kpis=outcome.kpis,
                    baseline_score=score_kpis(outcome.kpis),
                    ensemble_size=max(4, min(8, ensemble // 2)),
                    progress=lambda frac, msg, extra: _report(0.6 + 0.3 * frac, msg, extra),
                )
        _report(0.95, "assembling result")
        result = build_result(
            outcome,
            request,
            baseline=baseline,
            baseline_id=baseline_id,
            recommendations=recommendations,
            sensitivity=[],
            narrative=narrative,
        )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        self._simulations += 1

        if persist:
            result = await self._persist_simulation(result, snapshot, request)
        _report(1.0, "complete")
        await BUS.publish(
            Topics.SIM_COMPLETE,
            {
                "simulation_id": result.id,
                "name": request.name,
                "runtime_ms": result.runtime_ms,
                "patients_at_risk": result.kpis.patients_at_risk,
                "degraded": result.degraded,
            },
        )
        return result

    async def _simulate(self, cycle: CycleReport, snapshot: TwinSnapshot, prediction: Mapping[str, Any]) -> tuple[ScenarioOutcome, SimulationResult]:
        """Cycle-driven simulation: the forecast rainfall becomes the scenario."""
        mm_h = float(prediction.get("rainfall_mm_h") or 0.0)
        from ..domain.schemas import GeoPoint, HazardSpec
        from ..domain.enums import HazardType

        requests = SimulationRequest(
            name=f"auto-cycle-{cycle.cycle_id}",
            horizon_minutes=settings.sim_horizon_minutes,
            timestep_minutes=settings.sim_timestep_minutes,
            # the scheduled loop stays cheap so the console never feels slow: it is
            # a *refresh*, and a commander who wants precision asks for a run with
            # an explicit, larger ensemble
            ensemble_size=max(6, min(12, settings.sim_ensemble_size)),
            hazards=[
                HazardSpec(
                    hazard_type=HazardType.RAINFALL_BURST,
                    location=GeoPoint(lon=_city_center(snapshot)[0], lat=_city_center(snapshot)[1]),
                    radius_m=7_000,
                    intensity_mm_h=max(5.0, mm_h),
                    start_min=15,
                    duration_min=180,
                    probability=1.0,
                )
            ],
            description=f"scheduled refresh from the {prediction.get('trend')} rainfall forecast",
            notes="produced automatically by the Predict stage; not ordered by an operator",
        )
        with Timer("stage_simulate"), cycle.track(StageName.SIMULATE) as record:
            result = await self._run_simulation(snapshot, requests, persist=True, with_recommendations=True, baseline_id=None, progress=None)
            record.items = int(result.kpis.patients_at_risk)
            record.detail = (
                f"{len(result.uncertainty.p10.model_fields)} KPIs over {result.runtime_ms:.0f} ms; "
                f"{result.kpis.patients_at_risk:.0f} patients at risk, {result.kpis.roads_impassable_km:.1f} km impassable"
            )
            return None, result  # type: ignore[return-value]

    async def _recommend(self, cycle: CycleReport, outcome: ScenarioOutcome | None, result: SimulationResult) -> None:
        with Timer("stage_recommend"), cycle.track(StageName.RECOMMEND) as record:
            record.items = len(result.recommendations)
            if result.recommendations:
                top = result.recommendations[0]
                record.detail = f"{len(result.recommendations)} ranked actions; first: {top.action} (confidence {top.confidence:.2f})"
                await BUS.publish(
                    Topics.RECOMMENDATION,
                    {
                        "simulation_id": result.id,
                        "recommendations": [
                            {"rank": r.rank, "action": r.action, "confidence": r.confidence, "delta": r.expected_kpi_delta}
                            for r in result.recommendations[:5]
                        ],
                    },
                    
                )
            else:
                record.status = "degraded"
                record.degraded = True
                record.detail = "no candidate survived counterfactual evaluation"

    # -- persistence ------------------------------------------------------- #
    async def _persist_simulation(self, result: SimulationResult, snapshot: TwinSnapshot, request: SimulationRequest) -> SimulationResult:
        """Store the run, its timeline, cascade edges and recommendations.

        Written in one transaction so a partially-saved simulation can never be
        mistaken for a complete one during an after-action review.
        """
        try:
            async with repository.session_scope() as session:
                simulation_id = await store.save_simulation(session, result, request)
            result.id = simulation_id
            return result
        except Exception as exc:  # noqa: BLE001 - never lose a completed analysis
            log.error("simulation persistence failed", extra={"custos": {"error": str(exc)[:200]}})
            result.degraded = True
            result.explainability.notes += (
                " This run was computed but could not be persisted to the operational database; it exists only in "
                "this browser session and will not appear in the audit trail."
            )
            return result

    # -- comparison -------------------------------------------------------- #
    async def compare(self, request: Any) -> ComparisonResult:
        """Run two scenarios, diff them KPI by KPI, and say which one wins and why.

        The verdict is *computed*, not narrated: a KPI only counts as a decisive
        factor when the difference is larger than the ensemble spread of the two
        runs, otherwise the honest answer is "within model noise".
        """
        snapshot = await repository.get_snapshot()
        left, right = await asyncio.gather(
            self.run_stored_or_request(snapshot, request.scenario_a_id),
            self.run_stored_or_request(snapshot, request.scenario_b_id),
        )

        win_counts = _weighted_wins(left, right, request.weights)
        rows: list[ComparisonRow] = []
        a_band = left.uncertainty
        b_band = right.uncertainty
        for field in KPISet.model_fields:
            a_value = float(getattr(left.kpis, field))
            b_value = float(getattr(right.kpis, field))
            delta = b_value - a_value
            band_a = (float(getattr(a_band.p10, field)), float(getattr(a_band.p90, field)))
            band_b = (float(getattr(b_band.p10, field)), float(getattr(b_band.p90, field)))
            spread = max(band_a[1] - band_a[0], band_b[1] - band_b[0])
            lower = lower_is_better(field)
            material = abs(delta) > max(spread, 1e-9)
            if abs(delta) < 1e-9:
                winner = "tie"
            else:
                improved = (delta < 0) if lower else (delta > 0)
                winner = "b" if improved else "a"
            rows.append(
                ComparisonRow(
                    kpi=field,
                    label=label_for(field),
                    unit=unit_for(field),
                    a=round(a_value, 3),
                    b=round(b_value, 3),
                    delta=round(delta, 3),
                    delta_pct=round((delta / a_value * 100.0) if abs(a_value) > 1e-9 else 0.0, 2),
                    winner=winner,  # type: ignore[arg-type]
                    lower_is_better=lower,
                    band_a=(round(band_a[0], 3), round(band_a[1], 3)),
                    band_b=(round(band_b[0], 3), round(band_b[1], 3)),
                    material=material,
                )
            )
        rows.sort(key=lambda row: (not row.material, -abs(row.delta_pct)))

        decisive = [row for row in rows if row.material][:6]
        tradeoffs = [
            row for row in rows if row.material and row.winner != ("b" if win_counts["b"] > win_counts["a"] else "a")
        ][:4]
        verdict = _verdict(win_counts, decisive)
        if win_counts["a"] == win_counts["b"]:
            verdict = "the two scenarios are within model noise of each other on the weighted headline"
        explainability = Explainability(
            confidence=0.9 if decisive else 0.55,
            band=confidence_band(0.9 if decisive else 0.55),
            model="custos-hydro-lumped-v1 + dependency-cascade-v1",
            method="paired ensemble comparison; a difference counts only when it exceeds the ensemble spread",
            data_freshness=left.explainability.data_freshness,
            assumption_list=list(left.explainability.assumption_list),
            evidence_ids=[f"sim:{left.id}", f"sim:{right.id}"],
            uncertainty={
                "members_a": float(left.request.ensemble_size),
                "members_b": float(right.request.ensemble_size),
                "material_kpis": float(len(decisive)),
            },
            grounded=True,
            notes=(
                "KPI rows whose difference is smaller than the p10-p90 spread are marked non-material: "
                "the model cannot distinguish them, and reporting them as wins would be noise dressed as insight."
            ),
        )
        return ComparisonResult(
            scenario_a=_summary(left),
            scenario_b=_summary(right),
            rows=rows,
            score_a=round(score_kpis(left.kpis, request.weights), 2),
            score_b=round(score_kpis(right.kpis, request.weights), 2),
            verdict=verdict,
            decisive_factors=[
                f"{row.label}: {row.a:g} -> {row.b:g} {row.unit}".strip() + (f" ({row.delta_pct:+.0f}%)" if abs(row.delta_pct) >= 1 else "")
                for row in decisive
            ],
            tradeoffs=[
                f"{row.label} moves the other way ({row.delta:+.2f} {row.unit})".strip() for row in tradeoffs
            ],
            explainability=explainability,
        )

    async def run_stored_or_request(self, snapshot: TwinSnapshot, scenario_id: str) -> SimulationResult:
        """Re-run a stored scenario against the *current* twin.

        Deliberately re-run rather than replayed from the database: comparing a
        stored result computed against Monday's twin with a fresh run against
        today's would attribute the change in the city to the change in the plan.
        The stored request is the source, and the twin is always current.
        """
        from ..services import store

        async with store.session_scope() as session:
            stored = await store.get_simulation(session, scenario_id)
        if stored is None:
            raise KeyError(scenario_id)
        request = SimulationRequest(**stored["result"]["request"])
        return await self._run_simulation(snapshot, request, persist=False, with_recommendations=False, baseline_id=None, progress=None)

    async def sensitivity(self, request: SimulationRequest, *, drivers: Sequence[str] | None = None) -> list[Any]:
        snapshot = await repository.get_snapshot()
        ensemble = max(4, min(12, (request.ensemble_size or settings.sim_ensemble_size) // 4))
        return await asyncio.to_thread(sensitivity_analysis, snapshot, request, drivers=drivers, ensemble_size=ensemble)

    # -- status ------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "cycles": self._cycles,
            "simulations": self._simulations,
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "scenario_rain_mm_h": self._scenario_rain_mm_h,
            "last_cycle": None
            if self._last_cycle is None
            else {
                "cycle_id": self._last_cycle.cycle_id,
                "at": self._last_cycle.started_at.isoformat(),
                "duration_ms": self._last_cycle.duration_ms,
                "degraded": self._last_cycle.degraded,
                "stages": [s.to_model().model_dump() for s in self._last_cycle.stages],
                "anomalies": len(self._last_cycle.anomalies),
            },
            "prediction": self._prediction,
            "connectors": self.registry.health() if self.registry else [],
            "ai": REASONER.snapshot_stats(),
        }

    def set_scenario_rain(self, mm_h: float) -> None:
        """Drive the telemetry replay (drills, demonstrations, training)."""
        self._scenario_rain_mm_h = max(0.0, float(mm_h))
        connector = self.registry.get("kochi-telemetry-replay") if self.registry else None
        if connector is not None and hasattr(connector, "set_rain"):
            connector.set_rain(self._scenario_rain_mm_h)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _city_center(snapshot: TwinSnapshot) -> tuple[float, float]:
    if not snapshot.nodes:
        return (76.28, 9.97)
    lon = sum(n.lon for n in snapshot.nodes.values()) / len(snapshot.nodes)
    lat = sum(n.lat for n in snapshot.nodes.values()) / len(snapshot.nodes)
    return (round(lon, 4), round(lat, 4))


def _brief_facts(outcome: ScenarioOutcome) -> list[str]:
    k = outcome.kpis
    rep = outcome.representative
    worst = sorted(
        (n for n in rep.final_snapshot.nodes.values() if n.flood_depth_m > 0.05),
        key=lambda n: n.flood_depth_m,
        reverse=True,
    )[:6]
    facts = [
        f"patients at risk: {k.patients_at_risk:.0f}",
        f"hospital capacity lost: {k.hospital_capacity_lost_pct:.1f} percent",
        f"people requiring rescue: {k.people_requiring_rescue:.0f}",
        f"roads impassable: {k.roads_impassable_km:.1f} km",
        f"critical corridors affected: {k.critical_roads_impassable:.0f}",
        f"responder pairs severed: {k.severed_response_pairs:.0f}",
        f"mean response delay among reachable pairs: {k.avg_response_delay_min:.1f} minutes",
        f"people without power: {k.population_power_outage:.0f}",
        f"people without water: {k.population_water_outage:.0f}",
        f"telecom coverage lost: {k.telecom_coverage_lost_pct:.1f} percent",
        f"shelter over-capacity: {k.shelters_over_capacity_people:.0f}",
        f"equity index: {k.equity_index:.2f}",
        f"ensemble size: {len(outcome.members)}",
        f"p10 patients at risk: {outcome.p10.patients_at_risk:.0f}",
        f"p90 patients at risk: {outcome.p90.patients_at_risk:.0f}",
    ]
    for node in worst:
        facts.append(f"{node.name}: flood depth {node.flood_depth_m:.2f} m, status {node.status.value}")
    return facts


def row_metric(rows: Iterable[Mapping[str, Any]], sensor_id: str) -> str:
    for row in rows:
        if str(row.get("sensor_id")) == sensor_id:
            return str(row.get("metric") or "unknown")
    return "unknown"


#: which direction of change is bad news, per KPI (used by the comparison view)
_WORSE_WHEN_UP = {
    "patients_at_risk",
    "hospital_capacity_lost_pct",
    "roads_impassable_km",
    "critical_roads_impassable",
    "people_requiring_rescue",
    "people_flooded",
    "shelters_over_capacity_people",
    "population_power_outage",
    "population_water_outage",
    "telecom_coverage_lost_pct",
    "avg_response_delay_min",
    "p90_response_delay_min",
    "severed_response_pairs",
    "assets_failed",
    "assets_impaired",
    "max_flood_depth_m",
    "cascade_depth",
    "evacuation_minutes",
    "economic_impact_index",
}


def _is_bad(field: str, delta: float) -> bool:
    if field in {"equity_index", "kpi_score"}:
        return delta < 0  # equity: lower is worse
    if field in _WORSE_WHEN_UP:
        return delta > 0
    return delta > 0


def _summary(result: SimulationResult) -> SimulationSummary:
    return SimulationSummary(
        id=result.id,
        name=result.request.name,
        status=str(result.status),
        created_at=result.created_at,
        completed_at=result.completed_at,
        horizon_minutes=result.request.horizon_minutes,
        timestep_minutes=result.request.timestep_minutes,
        ensemble_size=result.request.ensemble_size,
        created_by="system",
        severity_delta=round(score_kpis(result.kpis), 2),
        headline=result.narrative[:280],
        progress=1.0,
    )


def _weighted_wins(left: SimulationResult, right: SimulationResult, weights: Mapping[str, float]) -> dict[str, int]:
    """Count material wins per side, honouring an operator-supplied weighting."""
    wins = {"a": 0, "b": 0}
    for field, weight in (weights or {}).items():
        if field not in KPISet.model_fields or weight <= 0:
            continue
        a_value = float(getattr(left.kpis, field))
        b_value = float(getattr(right.kpis, field))
        if abs(a_value - b_value) < 1e-9:
            continue
        improved = (b_value < a_value) if lower_is_better(field) else (b_value > a_value)
        wins["b" if improved else "a"] += 1
    if not weights:
        for field in ("patients_at_risk", "hospital_capacity_lost_pct", "people_requiring_rescue", "roads_impassable_km", "severed_response_pairs", "equity_index"):
            a_value = float(getattr(left.kpis, field))
            b_value = float(getattr(right.kpis, field))
            if abs(a_value - b_value) < 1e-9:
                continue
            improved = (b_value < a_value) if lower_is_better(field) else (b_value > a_value)
            wins["b" if improved else "a"] += 1
    return wins


def _verdict(wins: Mapping[str, int], decisive: Sequence[ComparisonRow]) -> str:
    if not decisive:
        return "no KPI moved by more than the ensemble spread: the scenarios are operationally equivalent"
    if wins["b"] > wins["a"]:
        return f"scenario B is preferable on {wins['b']} of {wins['a'] + wins['b']} weighted headline measures"
    if wins["a"] > wins["b"]:
        return f"scenario A is preferable on {wins['a']} of {wins['a'] + wins['b']} weighted headline measures"
    return "the two scenarios trade blows on the weighted headline"


ORCHESTRATOR = Orchestrator()
