/**
 * The commander's left rail: pipeline state, the forecast, the headline numbers,
 * and the things that are structurally dangerous about this city.
 *
 * The order is deliberate. Pipeline state first, because every number below it is
 * only as good as the last cycle. Then the forecast with its confidence and basis.
 * Then exposure. Then the dependency insights — the single points of failure are
 * the reason CUSTOS exists: they are the failures that are *not* visible in the
 * flood depth but decide whether the hospital keeps its power.
 */

"use client";

import { formatDepth, formatNumber, formatAgeFrom, titleise } from "@/lib/format";
import { useConsole } from "@/lib/store";

import { Chip, ConfidenceBadge, KpiCard, Panel } from "./primitives";
import { StageStrip } from "./StageStrip";

export function LeftRail() {
  const twin = useConsole((state) => state.twin);
  const stages = useConsole((state) => state.stages);
  const health = useConsole((state) => state.health);
  const anomalies = useConsole((state) => state.anomalies);
  const incidents = useConsole((state) => state.incidents);
  const tasks = useConsole((state) => state.tasks);
  const selectAsset = useConsole((state) => state.selectAsset);

  const prediction = (twin?.prediction ?? {}) as {
    rainfall_mm_h?: number;
    observed_rainfall_mm_h?: number;
    trend?: string;
    confidence?: number;
    band?: string;
    basis?: string;
    horizon_minutes?: number;
    stage_median_m?: number;
    single_points_of_failure?: string[];
  };

  const floodedZones = (twin?.zones ?? [])
    .filter((zone) => zone.flood_depth_m > 0.02)
    .sort((a, b) => b.flood_depth_m - a.flood_depth_m)
    .slice(0, 6);
  const spofs = twin?.insights.single_points_of_failure.slice(0, 5) ?? [];
  const worstAssets = (twin?.assets ?? [])
    .filter((asset) => asset.status === "failed" || asset.flood_depth_m > 0.1)
    .sort((a, b) => b.flood_depth_m - a.flood_depth_m)
    .slice(0, 6);

  const readings = twin?.sensors.filter((sensor) => sensor.last_value !== null).length ?? 0;
  const stale = twin?.sensors.filter((sensor) => (sensor.age_seconds ?? 0) > 900).length ?? 0;

  return (
    <div className="column">
      <Panel
        title="Operating loop"
        grow={false}
        actions={<Chip tone={stages.some((stage) => stage.state === "error") ? "tone-bad" : stages.some((stage) => stage.state === "degraded") ? "tone-warn" : "tone-ok"}>
          {stages.length}/6 stages
        </Chip>}
      >
        <StageStrip />
      </Panel>

      <Panel
        title="Forecast"
        grow={false}
        actions={<ConfidenceBadge confidence={prediction.confidence} band={prediction.band} />}
      >
        <div className="kpi-grid">
          <KpiCard
            label={`Rainfall intensity (next ${prediction.horizon_minutes ?? 30} min)`}
            value={formatNumber(prediction.rainfall_mm_h ?? null, "float")}
            unit="mm/h"
            band={`observed ${formatNumber(prediction.observed_rainfall_mm_h ?? null, "float")} mm/h · trend ${prediction.trend ?? "steady"}`}
            tone={(prediction.rainfall_mm_h ?? 0) >= 40 ? "tone-bad" : (prediction.rainfall_mm_h ?? 0) >= 12 ? "tone-warn" : "tone-ok"}
            meterPct={Math.min(100, ((prediction.rainfall_mm_h ?? 0) / 150) * 100)}
          />
          <div className="tiny mute">{prediction.basis ?? "no forecast basis reported"}</div>
        </div>
      </Panel>

      <Panel
        title="City state"
        grow={false}
        actions={<Chip tone="tone-mute">{twin ? `${twin.stats.nodes} assets` : "no twin"}</Chip>}
      >
        <div className="kpi-grid">
          <KpiCard
            label="Wards with water on the ground"
            value={formatNumber(floodedZones.length)}
            band={floodedZones.length ? `deepest ${formatDepth(floodedZones[0]!.flood_depth_m)} in ${floodedZones[0]!.name}` : "all wards dry"}
            tone={floodedZones.length ? "tone-warn" : "tone-ok"}
          />
          <KpiCard
            label="Telemetry"
            value={formatNumber(readings)}
            unit={`/ ${twin?.sensors.length ?? 0} sensors`}
            band={stale ? `${stale} silent for >15 min` : "every reporting sensor is fresh"}
            tone={stale ? "tone-warn" : "tone-ok"}
          />
          <KpiCard
            label="Open incidents"
            value={formatNumber(incidents.filter((incident) => !["resolved", "closed"].includes(incident.status)).length)}
            band={incidents.length ? `highest severity ${Math.max(...incidents.map((incident) => incident.severity))}` : "no incidents open"}
            tone={incidents.some((incident) => incident.severity >= 4) ? "tone-bad" : incidents.length ? "tone-warn" : "tone-ok"}
          />
          <KpiCard
            label="Committed tasks"
            value={formatNumber(tasks.filter((task) => task.status !== "done" && task.status !== "cancelled").length)}
            band={tasks.length ? `oldest ${formatAgeFrom(tasks[tasks.length - 1]!.created_at)}` : "nothing dispatched yet"}
          />
        </div>
      </Panel>

      {worstAssets.length ? (
        <Panel title="Worst assets right now" grow={false} actions={<Chip tone="tone-mute">live twin</Chip>}>
          <div className="list">
            {worstAssets.map((asset) => (
              <div className="item" key={asset.id} onClick={() => selectAsset(asset.id)}>
                <div className="item-head">
                  <span className="item-title">{asset.name}</span>
                  <Chip tone={asset.status === "failed" ? "tone-bad" : "tone-warn"}>{asset.status}</Chip>
                </div>
                <div className="item-meta">
                  <span className="mono">{formatDepth(asset.flood_depth_m)}</span>
                  <span>{titleise(asset.asset_type)}</span>
                  <span>service {(asset.service_level * 100).toFixed(0)}%</span>
                  {!asset.power_available ? <span className="tone-bad">no power</span> : null}
                  {!asset.access_available ? <span className="tone-bad">unreachable</span> : null}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      ) : null}

      {floodedZones.length ? (
        <Panel title="Ward water" grow={false}>
          <div className="list">
            {floodedZones.map((zone) => (
              <div className="row small" key={zone.id} style={{ justifyContent: "space-between" }}>
                <span>{zone.name}</span>
                <span className="mono">{formatDepth(zone.flood_depth_m)}</span>
              </div>
            ))}
          </div>
        </Panel>
      ) : null}

      <Panel
        title="Structural risk"
        grow={false}
        actions={<Chip tone="tone-info" title="Dependency analysis over 464 relational edges: two hops of cascade can reach a hospital from a substation.">SPOFs</Chip>}
      >
        {spofs.length === 0 ? (
          <div className="tiny mute">No single point of failure detected in the current graph.</div>
        ) : (
          <div className="list">
            {spofs.map((entry) => (
              <div className="item" key={entry.asset_id} onClick={() => selectAsset(entry.asset_id)} style={{ borderLeftColor: "var(--info)" }}>
                <div className="item-head">
                  <span className="item-title">{entry.name}</span>
                  <Chip tone="tone-info">reach {formatNumber(entry.reach)}</Chip>
                </div>
                <div className="item-meta">
                  <span>{titleise(entry.asset_type)}</span>
                  <span>{entry.dependents} direct dependents</span>
                  {entry.sole_provider_for.length ? <span className="tone-warn">sole provider for {entry.sole_provider_for.length}</span> : null}
                </div>
                <div className="tiny mute">{entry.rationale}</div>
              </div>
            ))}
          </div>
        )}
        <div className="tiny mute" style={{ marginTop: 6 }}>
          {twin?.insights.method ?? "dependency analysis unavailable"}
        </div>
      </Panel>

      {anomalies.length ? (
        <Panel title={`Anomalies (${anomalies.length})`} grow={false}>
          <div className="list">
            {anomalies.slice(0, 6).map((anomaly) => (
              <div className="row small" key={anomaly.id} style={{ justifyContent: "space-between" }}>
                <span>{anomaly.sensor_name}</span>
                <span className="mono tone-warn">z {anomaly.z.toFixed(1)}</span>
              </div>
            ))}
          </div>
          <div className="tiny mute" style={{ marginTop: 6 }}>
            Robust z (median + 1.4826·MAD), so a single spike cannot mask the anomaly it is part of.
          </div>
        </Panel>
      ) : null}

      {health ? (
        <Panel title="Subsystem health" grow={false} actions={<Chip tone={health.status === "ok" ? "tone-ok" : "tone-warn"}>{health.status}</Chip>}>
          <table className="table">
            <tbody>
              {Object.entries(health.checks).map(([name, check]) => (
                <tr key={name}>
                  <td className="tiny">{titleise(name)}</td>
                  <td className="tiny">
                    <Chip tone={String(check.status) === "ok" ? "tone-ok" : "tone-warn"}>{String(check.status ?? "—")}</Chip>
                  </td>
                  <td className="tiny mute">
                    {typeof check.latency_ms === "number" ? `${check.latency_ms} ms` : ""}
                    {typeof check.detail === "string" ? check.detail.slice(0, 40) : ""}
                    {typeof check.model === "string" ? check.model : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="tiny mute" style={{ marginTop: 6 }}>
            Uptime {formatNumber(health.uptime_s, "float")} s · version {health.version}
          </div>
        </Panel>
      ) : null}
    </div>
  );
}
