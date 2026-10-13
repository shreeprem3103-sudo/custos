/**
 * Evidence and explainability.
 *
 * This panel is the reason a commander should trust the screen. It contains, for
 * the thing currently selected (an asset, a run, or nothing in particular):
 *
 * * the live reading and its age, with the sensor that produced it;
 * * directly affected dependents — the cascade *inputs*, not just the outputs;
 * * the assumption register the model stood on for this run;
 * * the AI policy, including whether the local model is even reachable, because
 *   "the model is offline" must never be mistaken for "the model agrees";
 * * the audit chain check, on demand.
 */

"use client";

import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import { formatAgeFrom, formatDepth, formatNumber, statusTone, titleise } from "@/lib/format";
import { errorText, kpisFor, useConsole } from "@/lib/store";
import type { AnomalyOut, AuditEntryOut, AuditVerifyOut, GraphEdgeOut } from "@/lib/types";

import { Chip, ConfidenceBadge, Panel } from "./primitives";

export function EvidencePanel() {
  const twin = useConsole((state) => state.twin);
  const selectedAssetId = useConsole((state) => state.selectedAssetId);
  const simulation = useConsole((state) => state.simulation);
  const timelineIndex = useConsole((state) => state.timelineIndex);
  const ai = useConsole((state) => state.ai);
  const assumptions = useConsole((state) => state.assumptions);
  const anomalies = useConsole((state) => state.anomalies);
  const kpis = useConsole((state) => state.kpis);
  const toast = useConsole((state) => state.toast);

  const [neighbours, setNeighbours] = useState<GraphEdgeOut[]>([]);
  const [audit, setAudit] = useState<AuditEntryOut[]>([]);
  const [chain, setChain] = useState<AuditVerifyOut | null>(null);

  const asset = useMemo(() => twin?.assets.find((entry) => entry.id === selectedAssetId) ?? null, [twin, selectedAssetId]);
  const zone = useMemo(() => twin?.zones.find((entry) => entry.id === asset?.zone) ?? null, [twin, asset]);
  const sensors = useMemo(() => (twin?.sensors ?? []).filter((sensor) => sensor.asset_id === asset?.id || sensor.zone === asset?.zone), [twin, asset]);
  const result = simulation.result;
  const frameKpis = kpisFor(result, timelineIndex);
  const labelOf = useMemo(() => new Map(kpis.map((entry) => [entry.id, entry])), [kpis]);

  useEffect(() => {
    if (!selectedAssetId) {
      setNeighbours([]);
      return;
    }
    let cancelled = false;
    api<{ neighbours?: GraphEdgeOut[]; edges?: GraphEdgeOut[] }>(`/twin/assets/${selectedAssetId}`)
      .then((payload) => {
        if (!cancelled) setNeighbours(payload.neighbours ?? payload.edges ?? []);
      })
      .catch(() => {
        if (!cancelled) setNeighbours([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAssetId]);

  const assetAnomalies: AnomalyOut[] = useMemo(
    () => anomalies.filter((anomaly) => anomaly.asset_id === asset?.id || anomaly.zone === asset?.zone),
    [anomalies, asset],
  );

  async function refreshAudit() {
    try {
      const [entries, verify] = await Promise.all([
        api<AuditEntryOut[]>("/audit?limit=25"),
        api<AuditVerifyOut>("/audit/verify"),
      ]);
      setAudit(entries);
      setChain(verify);
      toast(verify.valid ? "success" : "error", verify.valid ? `Audit chain intact (${verify.entries_checked} entries)` : `Audit chain broken at seq ${verify.broken_at_seq}`, verify.valid ? `head ${verify.head.slice(0, 16)}` : "Investigate before trusting this trail.");
    } catch (error) {
      toast("error", "Audit read failed", errorText(error));
    }
  }

  return (
    <div className="column" style={{ overflow: "auto" }}>
      <Panel
        title={asset ? `Asset · ${asset.name}` : "Asset inspector"}
        grow={false}
        actions={asset ? <Chip tone={statusTone(asset.status)}>{asset.status}</Chip> : null}
      >
        {asset ? (
          <div style={{ display: "grid", gap: 7 }}>
            <div className="row small">
              <Chip tone="tone-info">{titleise(asset.asset_type)}</Chip>
              {asset.subtype ? <Chip>{asset.subtype}</Chip> : null}
              {zone ? <Chip>ward {zone.name}</Chip> : null}
              {asset.operator ? <Chip>{asset.operator}</Chip> : null}
            </div>
            <div className="grid-2">
              <div className="kpi">
                <div className="kpi-label">Flood depth at location</div>
                <div className="kpi-value">{formatDepth(asset.flood_depth_m)}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Service level</div>
                <div className="kpi-value">{(asset.service_level * 100).toFixed(0)}%</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Power · access</div>
                <div className="kpi-value" style={{ fontSize: 15 }}>
                  {asset.power_available ? "power up" : "no power"} · {asset.access_available ? "reachable" : "cut off"}
                </div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Criticality</div>
                <div className="kpi-value">{(asset.criticality * 100).toFixed(0)}%</div>
              </div>
            </div>

            <div className="tiny mute">
              state observed {formatAgeFrom(asset.updated_at)}
              {asset.provenance ? ` · source ${JSON.stringify(asset.provenance).slice(0, 90)}` : " · no provenance recorded (treated as unknown, not as fresh)"}
            </div>

            {Object.keys(asset.capacity ?? {}).length ? (
              <details className="details">
                <summary>Design capacity</summary>
                <div className="tiny mono" style={{ marginTop: 6 }}>
                  {Object.entries(asset.capacity).map(([key, value]) => (
                    <div key={key}>
                      {key}: {String(value)}
                    </div>
                  ))}
                </div>
              </details>
            ) : null}

            <div>
              <div className="upper tiny mute">direct dependency links ({neighbours.length})</div>
              {neighbours.length === 0 ? (
                <div className="tiny mute">No dependency edges read for this asset.</div>
              ) : (
                <table className="table" style={{ marginTop: 4 }}>
                  <thead>
                    <tr>
                      <th>direction</th>
                      <th>other end</th>
                      <th>relation</th>
                      <th>weight</th>
                      <th>lead</th>
                    </tr>
                  </thead>
                  <tbody>
                    {neighbours.slice(0, 24).map((edge) => {
                      const outgoing = edge.source_id === asset.id;
                      return (
                        <tr key={edge.id}>
                          <td className="tiny">{outgoing ? "→ feeds" : "← depends"}</td>
                          <td className="tiny mono">{outgoing ? edge.target_id : edge.source_id}</td>
                          <td className="tiny">{edge.kind}</td>
                          <td className="tiny mono">{edge.weight.toFixed(2)}</td>
                          <td className="tiny mono">{edge.lead_time_min} m</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>

            {sensors.length ? (
              <div>
                <div className="upper tiny mute">sensors in this ward</div>
                <table className="table" style={{ marginTop: 4 }}>
                  <thead>
                    <tr>
                      <th>sensor</th>
                      <th>metric</th>
                      <th>last</th>
                      <th>age</th>
                      <th>baseline</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sensors.slice(0, 12).map((sensor) => (
                      <tr key={sensor.id}>
                        <td className="tiny">{sensor.name}</td>
                        <td className="tiny mono">{sensor.metric}</td>
                        <td className="tiny mono">
                          {sensor.last_value === null ? "—" : `${formatNumber(sensor.last_value, "float")} ${sensor.unit}`}
                        </td>
                        <td className="tiny mono">{sensor.age_seconds === null ? "—" : `${Math.round(sensor.age_seconds)} s`}</td>
                        <td className="tiny mono">{sensor.baseline_median === null ? "—" : formatNumber(sensor.baseline_median, "float")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}

            {assetAnomalies.length ? (
              <div>
                <div className="upper tiny mute">anomalies in scope</div>
                {assetAnomalies.map((anomaly) => (
                  <div className="row small" key={anomaly.id}>
                    <Chip tone="tone-warn">z {anomaly.z.toFixed(1)}</Chip>
                    <span className="mono">
                      {anomaly.metric} {formatNumber(anomaly.value, "float")}
                    </span>
                    <span className="mute">baseline {formatNumber(anomaly.baseline, "float")} ± {formatNumber(anomaly.sigma, "float")}</span>
                    <span className="mute">{formatAgeFrom(anomaly.at)}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="empty">
            Click an asset, a road or a cascade arc on the map. The inspector reports the reading, its age, its dependencies and the
            anomalies in the same ward — the evidence behind the flag.
          </div>
        )}
      </Panel>

      {result ? (
        <Panel
          title="Run evidence"
          grow={false}
          actions={<ConfidenceBadge confidence={result.explainability.confidence} band={result.explainability.band} />}
        >
          <div style={{ display: "grid", gap: 6 }}>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Model</span>
              <span className="mono tiny">{result.explainability.model}</span>
            </div>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Data freshness</span>
              <span className="mono tiny">{result.explainability.data_freshness}</span>
            </div>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Grounded phrasing</span>
              <span className="mono tiny">{result.explainability.grounded ? "verified, figure by figure" : "template fallback used"}</span>
            </div>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Ensemble evidence ids</span>
              <span className="mono tiny">{result.explainability.evidence_ids.slice(0, 4).join(", ") || "—"}</span>
            </div>

            <div className="upper tiny mute" style={{ marginTop: 4 }}>
              KPI uncertainty, end state
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th>KPI</th>
                  <th>p10</th>
                  <th>p50</th>
                  <th>p90</th>
                  <th>window</th>
                  <th>state now</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(result.uncertainty.p50)
                  .slice(0, 18)
                  .map((key) => {
                    const definition = labelOf.get(key);
                    const isFloat = definition?.unit === "%" || definition?.unit === "km" || definition?.unit === "min" || definition?.unit === "index";
                    return (
                      <tr key={key}>
                        <td className="tiny">{definition?.label ?? titleise(key)}</td>
                        <td className="mono tiny">{formatNumber(result.uncertainty.p10[key], isFloat ? "float" : "count")}</td>
                        <td className="mono tiny bold">{formatNumber(result.uncertainty.p50[key], isFloat ? "float" : "count")}</td>
                        <td className="mono tiny">{formatNumber(result.uncertainty.p90[key], isFloat ? "float" : "count")}</td>
                        <td className="mono tiny mute">
                          {formatNumber((result.uncertainty.p90[key] ?? 0) - (result.uncertainty.p10[key] ?? 0), isFloat ? "float" : "count")}
                        </td>
                        <td className="mono tiny">{formatNumber(frameKpis[key], isFloat ? "float" : "count")}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>

            {result.explainability.assumption_list?.length ? (
              <details className="details" open>
                <summary>Assumptions this run stood on ({result.explainability.assumption_list.length})</summary>
                <ul className="tiny mute" style={{ margin: "6px 0 0", paddingLeft: 16 }}>
                  {result.explainability.assumption_list.slice(0, 12).map((assumption) => (
                    <li key={assumption}>{assumption}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        </Panel>
      ) : null}

      <Panel
        title="Model & AI policy"
        grow={false}
        actions={
          <Chip tone={ai?.ollama.reachable ? "tone-ok" : "tone-warn"}>
            {ai?.ollama.reachable ? "local model online" : "local model offline"}
          </Chip>
        }
      >
        {ai ? (
          <div style={{ display: "grid", gap: 6 }}>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Runtime</span>
              <span className="mono tiny">{ai.ollama.model} @ {ai.ollama.base_url}</span>
            </div>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Numbers from a language model</span>
              <span className="mono tiny">{ai.policy.allow_number_invention ? "PERMITTED (misconfigured)" : "never - engines only"}</span>
            </div>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Grounding</span>
              <span className="mono tiny">{ai.policy.grounding_required ? "every figure re-checked against the record" : "off"}</span>
            </div>
            <div className="row small" style={{ justifyContent: "space-between" }}>
              <span className="dim">Fallback</span>
              <span className="mono tiny">{ai.policy.fallback}</span>
            </div>
            <div className="tiny mute" style={{ marginTop: 4 }}>
              The predictions, cascades and recommendations on this screen are computed by the hydrology, dependency-cascade and routing
              engines. The language model only ever rephrases what those engines produced, and a rejected draft is logged and replaced —
              so a model outage changes the wording, never the numbers.
            </div>
          </div>
        ) : (
          <div className="empty">AI status unavailable.</div>
        )}
      </Panel>

      {assumptions ? (
        <Panel title="Assumption register" grow={false}>
          <div style={{ display: "grid", gap: 8 }}>
            {Object.entries(assumptions).map(([group, items]) => (
              <details className="details" key={group}>
                <summary>
                  {titleise(group)} ({items.length})
                </summary>
                <ul className="tiny mute" style={{ margin: "6px 0 0", paddingLeft: 16 }}>
                  {items.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        </Panel>
      ) : null}

      <Panel
        title="Audit trail"
        grow={false}
        actions={
          <button className="btn small" onClick={refreshAudit}>
            read & verify chain
          </button>
        }
        foot={
          chain ? (
            <Chip tone={chain.valid ? "tone-ok" : "tone-bad"}>
              {chain.valid ? `chain intact · ${chain.entries_checked} entries` : `BROKEN at seq ${chain.broken_at_seq}`}
            </Chip>
          ) : (
            <span className="tiny mute">Every login, refusal, cycle, simulation, commit and configuration change is chained.</span>
          )
        }
      >
        {audit.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>seq</th>
                <th>when</th>
                <th>actor</th>
                <th>action</th>
                <th>resource</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((entry) => (
                <tr key={entry.seq}>
                  <td className="mono tiny">{entry.seq}</td>
                  <td className="mono tiny">{formatAgeFrom(entry.at)}</td>
                  <td className="tiny">{entry.actor}</td>
                  <td className="tiny">{entry.action}</td>
                  <td className="tiny mono" title={entry.hash}>
                    {entry.resource.slice(0, 28)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="tiny mute">Read the chain to show the most recent 25 entries with their hash links.</div>
        )}
      </Panel>
    </div>
  );
}
