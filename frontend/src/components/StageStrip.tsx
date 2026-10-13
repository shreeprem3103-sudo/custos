/**
 * The six-stage operating loop, as the pipeline reports it.
 *
 * This is the console's heartbeat: if "predict" is idle or "simulate" is
 * degraded, every number elsewhere on the screen is older than it looks. The
 * stage names come from the server (which is the only writer of this view), so
 * the strip can never disagree with the backend about what ran.
 */

"use client";

import { formatAgeFrom, titleise } from "@/lib/format";
import { useConsole } from "@/lib/store";
import type { StageOut } from "@/lib/types";

const GLYPH: Record<string, string> = {
  active: "●",
  running: "◐",
  degraded: "▲",
  error: "✕",
  idle: "○",
};

const TONE: Record<string, string> = {
  active: "tone-ok",
  running: "tone-info",
  degraded: "tone-warn",
  error: "tone-bad",
  idle: "tone-mute",
};

function blockOf(stage: StageOut): string {
  return stage.stage.includes(":") ? stage.stage.split(":")[0]!.trim() : stage.stage;
}

export function StageStrip() {
  const stages = useConsole((state) => state.stages);
  const prediction = useConsole((state) => state.stages);
  const cycle = useConsole((state) => state.cycle);

  void prediction;

  if (!stages.length) {
    return <div className="empty">The pipeline has not reported a cycle yet.</div>;
  }

  return (
    <div className="stage-strip">
      {stages.map((stage) => {
        const state = stage.state ?? "idle";
        return (
          <div key={stage.stage} className={`stage ${state}`}>
            <span className={`glyph ${TONE[state] ?? "tone-mute"}`} title={`state: ${state}`}>
              {GLYPH[state] ?? "○"}
            </span>
            <span style={{ minWidth: 0 }}>
              <div className="stage-name">{titleise(blockOf(stage))}</div>
              <div className="stage-detail" title={stage.detail}>
                {stage.detail || "no detail reported"}
              </div>
            </span>
            <span className="tiny mute mono" title={stage.last_run_at ?? undefined}>
              {stage.last_run_at ? formatAgeFrom(stage.last_run_at) : "—"}
            </span>
          </div>
        );
      })}
      {cycle ? (
        <div className="tiny mute" style={{ marginTop: 6 }}>
          cycle {cycle.cycle_id} · {(cycle.duration_ms / 1000).toFixed(1)} s · {cycle.readings_ingested} readings ·{" "}
          {cycle.anomalies} anomalies {cycle.degraded ? "· degraded" : ""}
        </div>
      ) : null}
    </div>
  );
}
