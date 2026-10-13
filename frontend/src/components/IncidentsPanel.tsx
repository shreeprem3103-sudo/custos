/**
 * Incidents.
 *
 * Every incident is a claim made by *someone* — a sensor, a phone call, a patrol —
 * and the panel is built around that: the confidence, the reporter and the
 * evidence are shown before the narrative, and the triage action is deterministic
 * (the ladder is decided by severity, not by a language model) with the model
 * allowed only to phrase the rationale.
 */

"use client";

import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import { formatAgeFrom, formatNumber, severityTone, titleise } from "@/lib/format";
import { errorText, useConsole } from "@/lib/store";
import type { IncidentOut, TriageOut } from "@/lib/types";

import { Chip, ConfidenceBadge, Panel } from "./primitives";

const STATUS_TONE: Record<string, string> = {
  detected: "tone-warn",
  triaged: "tone-info",
  active: "tone-bad",
  contained: "tone-ok",
  resolved: "tone-ok",
  closed: "tone-mute",
};

export function IncidentsPanel() {
  const incidents = useConsole((state) => state.incidents);
  const selectedIncidentId = useConsole((state) => state.selectedIncidentId);
  const selectIncident = useConsole((state) => state.selectIncident);
  const triageIncident = useConsole((state) => state.triageIncident);
  const toast = useConsole((state) => state.toast);

  const [detail, setDetail] = useState<IncidentOut | null>(null);
  const [triage, setTriage] = useState<TriageOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);

  const selected = useMemo(() => incidents.find((incident) => incident.id === selectedIncidentId) ?? null, [incidents, selectedIncidentId]);

  useEffect(() => {
    if (!selectedIncidentId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    api<IncidentOut>(`/incidents/${selectedIncidentId}`)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch((error) => {
        if (!cancelled) toast("error", "Could not open the incident", errorText(error));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedIncidentId, toast]);

  const shown = detail ?? selected;

  return (
    <div className="column" style={{ overflow: "auto" }}>
      <Panel
        title={`Incident board (${incidents.length})`}
        grow={false}
        actions={
          <button className="btn small" onClick={() => setCreating((value) => !value)}>
            {creating ? "cancel" : "log an observation"}
          </button>
        }
      >
        {creating ? <CreateIncidentForm onDone={() => setCreating(false)} /> : null}
        <div className="list" style={{ marginTop: creating ? 10 : 0 }}>
          {incidents.length === 0 ? <div className="empty">No incidents open. The board is quiet, not broken.</div> : null}
          {incidents.map((incident) => (
            <div
              key={incident.id}
              className={`item ${incident.id === selectedIncidentId ? "selected" : ""}`}
              onClick={() => selectIncident(incident.id)}
              style={{ borderLeftColor: incident.severity >= 4 ? "var(--bad)" : incident.severity >= 3 ? "var(--warn)" : "var(--accent)" }}
            >
              <div className="item-head">
                <span className="item-title">{incident.title}</span>
                <Chip tone={severityTone(incident.severity)}>SEV {incident.severity}</Chip>
              </div>
              <div className="item-meta">
                <span className={STATUS_TONE[incident.status] ?? ""}>{titleise(incident.status)}</span>
                <span>{incident.category.replace(/_/g, " ")}</span>
                <span>confidence {(incident.confidence * 100).toFixed(0)}%</span>
                <span>detected {formatAgeFrom(incident.detected_at)}</span>
                {incident.zone ? <span>ward {incident.zone}</span> : null}
                {incident.population_exposed ? <span>{formatNumber(incident.population_exposed)} exposed</span> : null}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {shown ? (
        <Panel
          title="Incident detail"
          actions={
            <>
              <Chip tone={severityTone(shown.severity)}>SEV {shown.severity}</Chip>
              <Chip tone={STATUS_TONE[shown.status] ?? "tone-mute"}>{titleise(shown.status)}</Chip>
              <ConfidenceBadge confidence={shown.confidence} />
            </>
          }
          foot={
            <>
              <button
                className="btn primary small"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  const outcome = await triageIncident(shown.id);
                  setTriage(outcome);
                  setBusy(false);
                }}
              >
                {busy ? <span className="spin" /> : null}
                Run deterministic triage
              </button>
              <span className="tiny mute">
                The dispatch ladder is decided by severity and role rules in the API; the local model is allowed to phrase the rationale,
                never to choose the action.
              </span>
            </>
          }
        >
          <div style={{ display: "grid", gap: 8 }}>
            <p className="small dim" style={{ margin: 0 }}>
              {shown.summary}
            </p>

            <div className="row tiny mute">
              <span>reported by {shown.reported_by}</span>
              <span>at {shown.detected_at}</span>
              {shown.zone ? <span>ward {shown.zone}</span> : null}
              <span className="mono">
                {shown.lat.toFixed(4)} N, {shown.lon.toFixed(4)} E · r={Math.round(shown.radius_m)} m
              </span>
            </div>

            {Object.keys(shown.explainability ?? {}).length ? (
              <details className="details">
                <summary>Why the system believes this</summary>
                <pre className="evidence" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>
                  {JSON.stringify(shown.explainability, null, 1)}
                </pre>
              </details>
            ) : null}

            {shown.assets?.length ? (
              <div>
                <div className="upper tiny mute">linked assets</div>
                <div className="list" style={{ marginTop: 4 }}>
                  {shown.assets.map((link) => (
                    <div className="row small" key={link.asset_id}>
                      <Chip tone="tone-info">{link.role}</Chip>
                      <span className="mono">{link.asset_id}</span>
                      <span className="mute">impact weight {link.impact.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {triage ? (
              <div className="rec">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="bold">{titleise(triage.decision)}</span>
                  <Chip tone="tone-info">severity now {triage.severity_suggestion}</Chip>
                </div>
                <p className="small dim" style={{ margin: 0 }}>
                  {triage.rationale}
                </p>
                {triage.narration ? <div className="tiny mute">phrasing: {triage.narration}</div> : null}
                <ul className="small" style={{ margin: 0, paddingLeft: 16 }}>
                  {triage.actions.map((action) => (
                    <li key={action}>{action}</li>
                  ))}
                </ul>
                <span className="evidence">evidence {triage.evidence_ids.join(", ") || "recorder"}</span>
              </div>
            ) : null}

            {shown.timeline?.length ? (
              <div>
                <div className="upper tiny mute">timeline</div>
                <table className="table" style={{ marginTop: 4 }}>
                  <thead>
                    <tr>
                      <th>when</th>
                      <th>stage</th>
                      <th>who</th>
                      <th>what</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.timeline.map((entry) => (
                      <tr key={`${entry.at}-${entry.stage}`}>
                        <td className="mono tiny">{formatAgeFrom(entry.at)}</td>
                        <td className="tiny">{entry.stage}</td>
                        <td className="tiny">{entry.actor}</td>
                        <td className="small">{entry.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        </Panel>
      ) : (
        <Panel title="No incident selected" grow={false}>
          <div className="empty">Select an incident to see its evidence, linked assets and decision timeline.</div>
        </Panel>
      )}
    </div>
  );
}

function CreateIncidentForm({ onDone }: { onDone(): void }) {
  const twin = useConsole((state) => state.twin);
  const refreshOps = useConsole((state) => state.refreshOps);
  const toast = useConsole((state) => state.toast);
  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState(3);
  const [zone, setZone] = useState("");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const target = twin?.zones.find((entry) => entry.id === zone) ?? twin?.zones[0];
    if (!title.trim() || !target) {
      toast("warn", "Title and ward are required", "An incident without a location cannot be routed to anyone.");
      return;
    }
    setBusy(true);
    try {
      await api("/incidents", {
        method: "POST",
        body: {
          title: title.trim(),
          category: "urban_flood",
          severity,
          status: "detected",
          confidence: 0.5,
          summary: summary || "Operator-logged observation from the command console.",
          lon: target.centroid.lon,
          lat: target.centroid.lat,
          radius_m: 400,
          zone: target.id,
          population_exposed: Math.round(target.population * 0.05),
          reported_by: "operator:console",
        },
      });
      toast("success", "Observation logged", "It is now on the board with an audit entry and a timeline.");
      await refreshOps();
      onDone();
    } catch (error) {
      toast("error", "Could not log the observation", errorText(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="rec" onSubmit={submit}>
      <div className="field">
        <label>What was observed</label>
        <input type="text" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="e.g. Water entering the Kaloor underpass" />
      </div>
      <div className="grid-2">
        <div className="field">
          <label>Severity (1–5)</label>
          <input type="number" min={1} max={5} value={severity} onChange={(event) => setSeverity(Number(event.target.value))} />
        </div>
        <div className="field">
          <label>Ward</label>
          <select value={zone} onChange={(event) => setZone(event.target.value)}>
            <option value="">— select —</option>
            {(twin?.zones ?? []).map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="field">
        <label>Detail (optional)</label>
        <textarea rows={2} value={summary} onChange={(event) => setSummary(event.target.value)} />
      </div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="tiny mute">
          Operator observations carry a capped quality score: they are weighted below instrumented telemetry when the two disagree, and
          they are never silently discarded.
        </span>
        <button className="btn primary small" type="submit" disabled={busy}>
          {busy ? <span className="spin" /> : null}
          Log it
        </button>
      </div>
    </form>
  );
}
