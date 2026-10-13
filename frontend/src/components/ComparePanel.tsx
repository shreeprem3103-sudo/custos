/**
 * Scenario comparison.
 *
 * The question a decision board actually asks is "option A or option B, and how
 * sure are you?" — so this view refuses to answer with a single score. It shows,
 * per KPI: both point estimates, both ensemble bands, the difference, and whether
 * that difference is *material* (outside the combined p10–p90 spread of the two
 * runs). A row that is inside the noise is labelled as noise, in the table, not
 * in a footnote.
 */

"use client";

import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import { formatNumber, formatSigned, titleise } from "@/lib/format";
import { errorText, useConsole } from "@/lib/store";
import type { ComparisonOut } from "@/lib/types";

import { Chip, Panel } from "./primitives";

export function ComparePanel() {
  const compareA = useConsole((state) => state.compareA);
  const compareB = useConsole((state) => state.compareB);
  const setCompare = useConsole((state) => state.setCompare);
  const history = useConsole((state) => state.simulationHistory);
  const kpis = useConsole((state) => state.kpis);
  const toast = useConsole((state) => state.toast);
  const [comparison, setComparison] = useState<ComparisonOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const labels = useMemo(() => new Map(kpis.map((entry) => [entry.id, entry])), [kpis]);

  useEffect(() => {
    if (!compareA || !compareB) return;
    let cancelled = false;
    setBusy(true);
    setError(null);
    api<ComparisonOut>("/simulations/compare", {
      method: "POST",
      body: { scenario_a_id: compareA, scenario_b_id: compareB, weights: { patients_at_risk: 2, hospital_capacity_lost_pct: 1.5, shelters_over_capacity_people: 1.2 } },
    })
      .then((payload) => {
        if (!cancelled) setComparison(payload);
      })
      .catch((failure) => {
        if (!cancelled) {
          setError(errorText(failure));
          setComparison(null);
        }
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [compareA, compareB]);

  const runOptions = (
    <>
      <option value="">— pick a stored run —</option>
      {history.map((run) => (
        <option key={run.id} value={run.id}>
          {run.name} · {run.status}
          {run.degraded ? " · degraded" : ""} · {run.id}
        </option>
      ))}
    </>
  );

  const material = comparison?.rows.filter((row) => row.material) ?? [];

  return (
    <div className="column" style={{ overflow: "auto" }}>
      <Panel
        title="Scenario comparison"
        grow={false}
        actions={
          <>
            {busy ? <span className="spin" /> : null}
            <button
              className="btn small"
              onClick={() => {
                if (compareA && compareB) toast("info", "Re-reading comparison", `${compareA} vs ${compareB}`);
              }}
            >
              refresh
            </button>
          </>
        }
        foot={
          <span className="tiny mute">
            Two runs are only comparable when the same stress is applied to both legs. CUSTOS differences every recommendation against an
            equal-size, same-seed control run for exactly this reason.
          </span>
        }
      >
        <div className="grid-2">
          <div className="field">
            <label>Scenario A (baseline / do nothing)</label>
            <select value={compareA ?? ""} onChange={(event) => setCompare("a", event.target.value || null)}>
              {runOptions}
            </select>
          </div>
          <div className="field">
            <label>Scenario B (with interventions)</label>
            <select value={compareB ?? ""} onChange={(event) => setCompare("b", event.target.value || null)}>
              {runOptions}
            </select>
          </div>
        </div>
      </Panel>

      {error ? <div className="bad-banner">{error}</div> : null}

      {comparison ? (
        <>
          <Panel
            title="Verdict"
            grow={false}
            actions={
              <>
                <Chip tone="tone-info">{comparison.material_count} of {comparison.rows.length} KPIs differ materially</Chip>
                <Chip tone="tone-mute" title="Weighted headline: lower is better on the weighted KPI basket.">
                  A {formatNumber(comparison.weighted_score_a, "float")} vs B {formatNumber(comparison.weighted_score_b, "float")}
                </Chip>
              </>
            }
          >
            <p className="small" style={{ margin: 0 }}>
              {comparison.verdict}
            </p>
            <div className="tiny mute" style={{ marginTop: 6 }}>
              {comparison.method}
            </div>
          </Panel>

          <Panel title="KPI by KPI" bodyClass="tight" foot={<span className="tiny mute">Material rows (blue) differ by more than the combined ensemble spread. Everything else is inside the model's own noise and must not be presented to a decision board as a difference.</span>}>
            <table className="table">
              <thead>
                <tr>
                  <th>KPI</th>
                  <th>A</th>
                  <th>B</th>
                  <th>Δ (B−A)</th>
                  <th>Material?</th>
                </tr>
              </thead>
              <tbody>
                {comparison.rows.map((row) => {
                  const definition = labels.get(row.kpi);
                  const better = row.better;
                  return (
                    <tr key={row.kpi} className={row.material ? "material" : ""}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{definition?.label ?? row.label}</div>
                        <div className="tiny mute">{definition?.unit ?? row.unit}</div>
                      </td>
                      <td className={`mono ${better === "a" ? "better tone-ok" : ""}`}>
                        {formatNumber(row.value_a, row.unit === "%" || row.unit === "km" || row.unit === "min" ? "float" : "count")}
                        <div className="tiny mute">
                          {formatNumber(row.band_a[0], "float")}–{formatNumber(row.band_a[1], "float")}
                        </div>
                      </td>
                      <td className={`mono ${better === "b" ? "better tone-ok" : ""}`}>
                        {formatNumber(row.value_b, row.unit === "%" || row.unit === "km" || row.unit === "min" ? "float" : "count")}
                        <div className="tiny mute">
                          {formatNumber(row.band_b[0], "float")}–{formatNumber(row.band_b[1], "float")}
                        </div>
                      </td>
                      <td className={`mono ${row.delta < 0 === row.lower_is_better && row.delta !== 0 ? "tone-ok" : row.delta === 0 ? "" : "tone-bad"}`}>
                        {formatSigned(row.delta, row.unit === "%" || row.unit === "km" || row.unit === "min" ? "float" : "count")}
                        <div className="tiny mute">{row.delta_pct.toFixed(1)}%</div>
                      </td>
                      <td>
                        {row.material ? (
                          <Chip tone={better === "neither" ? "tone-mute" : "tone-ok"}>{better === "neither" ? "material, ambiguous" : `material → ${better.toUpperCase()} better`}</Chip>
                        ) : (
                          <Chip tone="tone-mute" title="Difference is inside the ensemble spread of both runs.">inside noise</Chip>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>

          <Panel title="Material differences only" grow={false}>
            {material.length === 0 ? (
              <div className="empty">
                Nothing differs beyond model noise. That is a genuine finding: at this severity the interventions tested did not move the
                city's outcome, and committing scarce resources on the strength of this run would be unjustified.
              </div>
            ) : (
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                {material.map((row) => (
                  <li key={row.kpi}>
                    <strong>{labels.get(row.kpi)?.label ?? titleise(row.kpi)}</strong>: {formatSigned(row.delta, "count")} {row.unit} ({row.delta_pct.toFixed(1)}%), band{" "}
                    {formatNumber(row.band_a[0], "float")}–{formatNumber(row.band_a[1], "float")} vs {formatNumber(row.band_b[0], "float")}–
                    {formatNumber(row.band_b[1], "float")}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </>
      ) : (
        <Panel title="Nothing selected" grow={false}>
          <div className="empty">
            Pick two stored runs above. Runs appear here as soon as they finish — including the ones the scheduled cycle launches on its
            own.
          </div>
        </Panel>
      )}
    </div>
  );
}
