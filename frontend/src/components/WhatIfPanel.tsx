/**
 * The What-If engine.
 *
 * This is the panel the whole product exists for: a commander states a scenario
 * ("this road floods in twenty minutes"), the engine runs a coupled ensemble, and
 * the console shows — in this order — what happens, how sure the model is, what
 * it would recommend, and what changed the last time an operator did something
 * like this.
 *
 * Three things are non-negotiable in the layout:
 *
 * 1. **The scenario is stated back in plain language** before it runs, including
 *    its limits, so nobody can mistake a what-if for a forecast.
 * 2. **Every headline figure sits next to its ensemble band.** A single number
 *    from a stochastic model is a lie of omission.
 * 3. **Recommendations show the counterfactual that produced them.** The
 *    delta column is a measured difference against a control run, not an opinion,
 *    and the label says which control.
 */

"use client";

import { useMemo, useState } from "react";

import { formatDepth, formatNumber, formatSigned, titleise } from "@/lib/format";
import { kpisFor, useConsole } from "@/lib/store";
import type { HazardSpec, Recommendation } from "@/lib/types";

import { Chip, ConfidenceBadge, Field, KpiCard, Panel, ProgressBar } from "./primitives";

const HEADLINE_KPIS: [string, string, string][] = [
  ["patients_at_risk", "Patients at risk", "people"],
  ["hospital_capacity_lost_pct", "Hospital capacity lost", "%"],
  ["people_requiring_rescue", "People requiring rescue", "people"],
  ["shelters_over_capacity_people", "Shelter shortfall", "people"],
  ["population_power_outage", "Without power", "people"],
  ["roads_impassable_km", "Roads impassable", "km"],
  ["severed_response_pairs", "Severed responder pairs", "pairs"],
  ["avg_response_delay_min", "Avg response delay", "min"],
];

/** Ward identifier → readable name, for the scenario builder's pickers. */
function wardOptions(zones: { id: string; name: string; population: number }[]): [string, string][] {
  return zones.map((zone) => [zone.id, `${zone.name} (${formatNumber(zone.population, "compact")} people)`]);
}

export function WhatIfPanel() {
  const scenario = useConsole((state) => state.scenario);
  const updateScenario = useConsole((state) => state.updateScenario);
  const resetScenario = useConsole((state) => state.resetScenario);
  const simulation = useConsole((state) => state.simulation);
  const startSimulation = useConsole((state) => state.startSimulation);
  const timelineIndex = useConsole((state) => state.timelineIndex);
  const twin = useConsole((state) => state.twin);
  const kpiDictionary = useConsole((state) => state.kpis);
  const commitRecommendation = useConsole((state) => state.commitRecommendation);

  const [assignTo, setAssignTo] = useState("ward-1");
  const [committed, setCommitted] = useState<Record<string, string>>({});

  const result = simulation.result;
  const kpis = kpisFor(result, timelineIndex);
  const labels = useMemo(() => new Map(kpiDictionary.map((entry) => [entry.id, entry])), [kpiDictionary]);

  const rainfall = scenario.hazards?.find((hazard) => hazard.hazard_type === "rainfall_burst");
  const surge = scenario.hazards?.find((hazard) => hazard.hazard_type === "tidal_surge");

  function patchHazard(kind: "rainfall_burst" | "tidal_surge", patch: Partial<HazardSpec>) {
    const hazards = [...(scenario.hazards ?? [])];
    const index = hazards.findIndex((hazard) => hazard.hazard_type === kind);
    if (index >= 0) hazards[index] = { ...hazards[index]!, ...patch };
    else hazards.push({ hazard_type: kind, ...patch });
    updateScenario({ hazards });
  }

  function removeHazard(kind: string) {
    updateScenario({ hazards: (scenario.hazards ?? []).filter((hazard) => hazard.hazard_type !== kind) });
  }

  const busy = simulation.loading || simulation.status === "running" || simulation.status === "submitting";

  return (
    <div className="column" style={{ overflow: "auto" }}>
      <Panel
        title="What-If scenario"
        actions={
          <>
            <button className="btn small ghost" onClick={() => resetScenario("calm")} title="A zero-forcing control run. Every KPI must read zero — if it does not, the model is wrong.">
              Control run
            </button>
            <button className="btn small ghost" onClick={() => resetScenario("monsoon")} title="A monsoon system with a spring tide: extreme, but both 2018 and 2019 exceeded it.">
              Monsoon preset
            </button>
            <button className="btn small ghost" onClick={() => resetScenario("burst")} title="A short, intense convective burst over the central wards.">
              Burst preset
            </button>
          </>
        }
        foot={
          <>
            <button className="btn primary" onClick={startSimulation} disabled={busy} style={{ minWidth: 156 }}>
              {busy ? <span className="spin" /> : null}
              {busy ? "Running ensemble…" : `Run ${scenario.ensemble_size}-member ensemble`}
            </button>
            <span className="tiny mute">
              {scenario.horizon_minutes} min horizon · {scenario.timestep_minutes} min steps · seed {scenario.random_seed}
            </span>
          </>
        }
      >
        <div style={{ display: "grid", gap: 9 }}>
          <Field label="Scenario name">
            <input
              type="text"
              value={scenario.name}
              onChange={(event) => updateScenario({ name: event.target.value })}
              placeholder="What-If: …"
            />
          </Field>

          <div className="grid-3">
            <Field label="Horizon (min)" hint="How far ahead the ensemble runs.">
              <input
                type="number"
                min={30}
                max={1440}
                step={30}
                value={scenario.horizon_minutes}
                onChange={(event) => updateScenario({ horizon_minutes: Number(event.target.value) })}
              />
            </Field>
            <Field label="Timestep (min)">
              <input
                type="number"
                min={5}
                max={120}
                step={5}
                value={scenario.timestep_minutes}
                onChange={(event) => updateScenario({ timestep_minutes: Number(event.target.value) })}
              />
            </Field>
            <Field label="Ensemble" hint="More members = tighter uncertainty band, longer runtime. 24-32 is the operational sweet spot.">
              <input
                type="number"
                min={1}
                max={64}
                step={1}
                value={scenario.ensemble_size}
                onChange={(event) => updateScenario({ ensemble_size: Number(event.target.value) })}
              />
            </Field>
          </div>

          <div className="details">
            <summary>Rainfall forcing</summary>
            <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small dim">Intensity</span>
                <span className="mono small">{Number(rainfall?.intensity_mm_h ?? 0).toFixed(0)} mm/h</span>
              </div>
              <input
                type="range"
                min={0}
                max={200}
                step={2}
                value={Number(rainfall?.intensity_mm_h ?? 0)}
                onChange={(event) =>
                  patchHazard("rainfall_burst", {
                    hazard_type: "rainfall_burst",
                    intensity_mm_h: Number(event.target.value),
                  })
                }
              />
              <div className="tiny mute">
                15 mm/h starts to load the network; the model shows a sharp knee near the ward's declared design rainfall (~44 mm/h)
                where ponding switches on abruptly.
              </div>
              <div className="grid-2">
                <Field label="Start (min)">
                  <input
                    type="number"
                    min={0}
                    max={720}
                    step={15}
                    value={Number(rainfall?.start_min ?? 0)}
                    onChange={(event) => patchHazard("rainfall_burst", { start_min: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Duration (min)">
                  <input
                    type="number"
                    min={15}
                    max={720}
                    step={15}
                    value={Number(rainfall?.duration_min ?? 180)}
                    onChange={(event) => patchHazard("rainfall_burst", { duration_min: Number(event.target.value) })}
                  />
                </Field>
              </div>
              <Field label="Storm centre (ward)">
                <select
                  value={scenario.hazards?.find((hazard) => hazard.location)?.zone_id ?? ""}
                  onChange={(event) => {
                    const zone = twin?.zones.find((entry) => entry.id === event.target.value);
                    if (zone) patchHazard("rainfall_burst", { location: zone.centroid, zone_id: zone.id });
                    else patchHazard("rainfall_burst", { location: { lon: 76.296, lat: 9.978 }, zone_id: null });
                  }}
                >
                  <option value="">City-wide (centroid 9.978 N, 76.296 E)</option>
                  {wardOptions(twin?.zones ?? []).map(([id, label]) => (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>
              {rainfall ? (
                <button className="btn small ghost" onClick={() => removeHazard("rainfall_burst")}>
                  remove rainfall forcing
                </button>
              ) : null}
            </div>
          </div>

          <div className="details">
            <summary>Tide / storm surge</summary>
            <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small dim">Surge above chart datum</span>
                <span className="mono small">{Number(surge?.surge_m ?? 0).toFixed(2)} m</span>
              </div>
              <input
                type="range"
                min={0}
                max={2}
                step={0.05}
                value={Number(surge?.surge_m ?? 0)}
                onChange={(event) => patchHazard("tidal_surge", { hazard_type: "tidal_surge", surge_m: Number(event.target.value) })}
              />
              <div className="tiny mute">
                The tide matters more than the rain on the western wards: gravity outfalls stop working when the backwater is higher
                than the canal, and pumping becomes the only defence.
              </div>
              {surge ? (
                <button className="btn small ghost" onClick={() => removeHazard("tidal_surge")}>
                  remove surge
                </button>
              ) : (
                <button className="btn small" onClick={() => patchHazard("tidal_surge", { hazard_type: "tidal_surge", surge_m: 0.25 })}>
                  add surge
                </button>
              )}
            </div>
          </div>

          <div className="details">
            <summary>Interventions to test ({scenario.interventions?.length ?? 0})</summary>
            <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
              <div className="tiny mute">
                Interventions are applied inside the simulation, not to the live city. Add one here to measure it in the same ensemble
                that produced the baseline numbers, which is the only way the difference means anything.
              </div>
              <InterventionAdder />
              {(scenario.interventions ?? []).map((intervention, index) => (
                <div className="row small" key={`${intervention.intervention_type}-${index}`}>
                  <Chip tone="tone-info">{titleise(intervention.intervention_type)}</Chip>
                  <span className="dim">
                    {intervention.zone_id ?? intervention.asset_id ?? "city-wide"}
                    {intervention.quantity ? ` · ${intervention.quantity}` : ""}
                  </span>
                  <button
                    className="btn small ghost"
                    onClick={() => updateScenario({ interventions: (scenario.interventions ?? []).filter((_, i) => i !== index) })}
                  >
                    remove
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </Panel>

      {simulation.status !== "idle" ? (
        <Panel
          title="Run status"
          grow={false}
          actions={
            <>
              <Chip tone={busy ? "tone-info" : simulation.status === "complete" ? "tone-ok" : "tone-bad"}>{simulation.status}</Chip>
              {simulation.runId ? <Chip tone="tone-mute">{simulation.runId}</Chip> : null}
            </>
          }
        >
          <div style={{ display: "grid", gap: 6 }}>
            {busy ? <ProgressBar value={simulation.progress} label={simulation.message} /> : null}
            {simulation.error ? <div className="bad-banner">{simulation.error}</div> : null}
            {simulation.status === "timeout" ? <div className="warn-banner">{simulation.message}</div> : null}
          </div>
        </Panel>
      ) : null}

      {result ? (
        <>
          <Panel
            title={`Outcome · ${timelineIndex === null ? "end state" : `t+${result.timeline[timelineIndex]?.t_min ?? 0} min`}`}
            actions={
              <>
                <ConfidenceBadge confidence={result.explainability.confidence} band={result.explainability.band} />
                <Chip tone={result.degraded ? "tone-warn" : "tone-ok"} title="A run is marked degraded when part of the inputs were stale or missing.">
                  {result.degraded ? "degraded inputs" : "inputs fresh"}
                </Chip>
                <Chip tone="tone-mute" title="Wall-clock runtime of the ensemble on this appliance.">
                  {Math.round(result.runtime_ms / 1000)} s
                </Chip>
              </>
            }
            foot={
              <>
                <Chip tone="tone-info" title="Reproducibility token: the same request and the same seed produce the same numbers. Disagreement between two identical runs would be a defect.">
                  determinism {result.determinism_token.slice(0, 16)}
                </Chip>
                <span className="tiny mute">
                  {result.explainability.model} · {result.explainability.data_freshness} inputs
                </span>
              </>
            }
          >
            <div className="kpi-grid">
              {HEADLINE_KPIS.map(([id, fallback, unit]) => {
                const definition = labels.get(id);
                const value = kpis[id];
                const p10 = timelineIndex === null ? result.uncertainty.p10[id] : undefined;
                const p90 = timelineIndex === null ? result.uncertainty.p90[id] : undefined;
                const baseline = result.baseline_kpis?.[id];
                const delta = baseline !== undefined && value !== undefined ? value - baseline : undefined;
                const band = p10 !== undefined && p90 !== undefined ? `${formatNumber(p10, unit === "%" ? "float" : "count")} – ${formatNumber(p90, unit === "%" ? "float" : "count")} ${unit}` : undefined;
                return (
                  <KpiCard
                    key={id}
                    label={definition?.label ?? fallback}
                    value={formatNumber(value, unit === "%" || unit === "km" || unit === "min" ? "float" : "count")}
                    unit={unit}
                    band={band}
                    delta={delta !== undefined ? formatSigned(delta, unit === "%" || unit === "km" || unit === "min" ? "float" : "count") : undefined}
                    deltaLabel={baseline !== undefined ? `against the equal-size control run (${formatNumber(baseline, "float")} ${unit})` : undefined}
                    tone={delta === undefined ? undefined : delta < 0 ? "tone-ok" : delta > 0 ? "tone-bad" : undefined}
                    hint={definition?.description}
                  />
                );
              })}
            </div>

            <div style={{ marginTop: 10 }}>
              <div className="upper tiny mute">narrative (grounded, re-checked figure by figure)</div>
              <p className="small dim" style={{ marginTop: 4 }}>
                {result.narrative}
              </p>
            </div>
          </Panel>

          <Panel
            title={`Recommendations (${result.recommendations.length})`}
            actions={<Chip tone={result.explainability.grounded ? "tone-ok" : "tone-warn"}>{result.explainability.grounded ? "grounded" : "phrasing fallback"}</Chip>}
            foot={
              <div className="row">
                <Field label="Assign to">
                  <input type="text" value={assignTo} onChange={(event) => setAssignTo(event.target.value)} style={{ width: 130 }} />
                </Field>
                <span className="tiny mute">Committing writes a task, reserves the listed resources, and appends to the audit chain.</span>
              </div>
            }
          >
            <div className="list">
              {result.recommendations.map((recommendation) => (
                <RecommendationCard
                  key={recommendation.id}
                  recommendation={recommendation}
                  committedTaskId={committed[recommendation.id]}
                  onCommit={async () => {
                    const task = await commitRecommendation(recommendation, assignTo || null);
                    if (task) setCommitted((previous) => ({ ...previous, [recommendation.id]: task.id }));
                  }}
                />
              ))}
              {result.recommendations.length === 0 ? (
                <div className="empty">
                  The engine found nothing worth doing at this severity. It is a legitimate answer: acting without a measured benefit
                  moves resources away from where they help.
                </div>
              ) : null}
            </div>
          </Panel>

          <Panel title="Sensitivity & cascade depth" grow={false}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small dim">Cascade edges in this run</span>
              <span className="mono">{result.cascade.length}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small dim">Deepest cascade hop</span>
              <span className="mono">{Math.max(0, ...result.cascade.map((impact) => impact.hop))}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small dim">Isolation events (assets cut off)</span>
              <span className="mono">{result.mobility.isolation_events}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small dim">Peak depth anywhere in the city</span>
              <span className="mono">{formatDepth(Math.max(0, ...result.timeline.map((frame) => frame.max_depth_m)))}</span>
            </div>
            <div className="tiny mute" style={{ marginTop: 6 }}>
              {result.explainability.method}
            </div>
          </Panel>
        </>
      ) : (
        <Panel title="How to read this" grow={false}>
          <ol className="small dim" style={{ paddingLeft: 18, margin: 0, display: "grid", gap: 4 }}>
            <li>State the scenario on the left. Rainfall drives ponding; surge decides whether the outfalls work at all.</li>
            <li>Run the ensemble. Every headline number arrives with a 10th–90th percentile band; treat the band, not the point.</li>
            <li>Use the map scrubber to step through the scenario in time and watch where the cascade lands.</li>
            <li>Read the recommendations — each one carries the counterfactual run that justifies it, and can be committed to the field from here.</li>
            <li>Compare the run against another stored run (Compare tab) before committing scarce resources to it.</li>
          </ol>
        </Panel>
      )}
    </div>
  );
}

function RecommendationCard({
  recommendation,
  onCommit,
  committedTaskId,
}: {
  recommendation: Recommendation;
  onCommit(): Promise<void> | void;
  committedTaskId?: string;
}) {
  const [busy, setBusy] = useState(false);
  const deltas = Object.entries(recommendation.expected_kpi_delta).filter(([, value]) => Math.abs(value) > 1e-9);

  return (
    <div className="rec">
      <div className="rec-head">
        <span className="rank">#{recommendation.rank}</span>
        <span className="item-title">{recommendation.action}</span>
      </div>
      <div className="item-meta">
        <span>window {recommendation.window}</span>
        <span title="How quickly this can actually be done, given staffing and access.">feasibility {(recommendation.feasibility * 100).toFixed(0)}%</span>
        <span title="How much of the outcome hangs on this lever in this scenario.">urgency {(recommendation.urgency * 100).toFixed(0)}%</span>
        <span title="Confidence that the measured effect survives ensemble noise.">confidence {(recommendation.confidence * 100).toFixed(0)}%</span>
      </div>
      <p className="small dim" style={{ margin: 0 }}>
        {recommendation.rationale}
      </p>
      <div className="counterfactual">
        <strong>Measured against:</strong> {recommendation.counterfactual}
      </div>
      {deltas.length ? (
        <div className="delta-grid">
          {deltas.map(([key, value]) => (
            <span key={key} className={value < 0 ? "tone-ok" : "tone-bad"}>
              {titleise(key)} {formatSigned(value, key.endsWith("_km") || key.endsWith("_min") || key.endsWith("_pct") ? "float" : "count")}
            </span>
          ))}
        </div>
      ) : (
        <div className="tiny mute">
          No measurable effect on any KPI at this severity. Recommended only because it is cheap and it insures against a worse peak —
          that is the honest reading, and it is stated rather than dressed up.
        </div>
      )}
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="evidence">
          evidence {recommendation.evidence_ids.slice(0, 2).join(", ") || "model-internal"} · generated by {recommendation.generated_by}
        </span>
        {committedTaskId ? (
          <Chip tone="tone-ok">committed · {committedTaskId}</Chip>
        ) : (
          <button
            className="btn small"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await onCommit();
              setBusy(false);
            }}
          >
            {busy ? <span className="spin" /> : null}
            Commit to field
          </button>
        )}
      </div>
      {recommendation.assumptions?.length ? (
        <details className="details">
          <summary>Assumptions this rests on ({recommendation.assumptions.length})</summary>
          <ul className="tiny mute" style={{ margin: "6px 0 0", paddingLeft: 16 }}>
            {recommendation.assumptions.map((assumption) => (
              <li key={assumption}>{assumption}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function InterventionAdder() {
  const twin = useConsole((state) => state.twin);
  const scenario = useConsole((state) => state.scenario);
  const updateScenario = useConsole((state) => state.updateScenario);
  const [kind, setKind] = useState("mobile_pump");
  const [zone, setZone] = useState("");
  const [quantity, setQuantity] = useState(2);

  const KINDS: [string, string][] = [
    ["mobile_pump", "Mobile pump set (lifts ward water)"],
    ["open_shelter", "Open shelter / annex capacity"],
    ["preposition_team", "Pre-position a rescue team"],
    ["contingency_power", "Contingency power for a facility"],
    ["alert_public", "Public alert / warning"],
    ["fleet_readiness", "Fleet readiness (responder availability)"],
  ];

  return (
    <div className="row">
      <select value={kind} onChange={(event) => setKind(event.target.value)} style={{ width: 220 }}>
        {KINDS.map(([id, label]) => (
          <option key={id} value={id}>
            {label}
          </option>
        ))}
      </select>
      <select value={zone} onChange={(event) => setZone(event.target.value)} style={{ width: 190 }}>
        <option value="">whole city</option>
        {wardOptions(twin?.zones ?? []).map(([id, label]) => (
          <option key={id} value={id}>
            {label}
          </option>
        ))}
      </select>
      <input
        type="number"
        min={1}
        max={200}
        value={quantity}
        onChange={(event) => setQuantity(Number(event.target.value))}
        style={{ width: 70 }}
        title="Units of the intervention (pumps, teams, places, hours)."
      />
      <button
        className="btn small"
        onClick={() =>
          updateScenario({
            interventions: [
              ...(scenario.interventions ?? []),
              {
                intervention_type: kind,
                zone_id: zone || null,
                quantity,
                // the engine reads these canonical parameters; the form only asks
                // for a quantity and translates it here
                capacity_m3s: kind === "mobile_pump" ? 0.35 * quantity : null,
                capacity_people: kind === "open_shelter" ? 1000 * quantity : null,
                description: `${quantity} × ${titleise(kind)}${zone ? ` in ${zone}` : ""}`,
              },
            ],
          })
        }
      >
        add
      </button>
    </div>
  );
}
