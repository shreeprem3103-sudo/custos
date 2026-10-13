/**
 * The status bar.
 *
 * Everything an operator needs before they trust anything below it: which city,
 * how fresh the twin is, whether the appliance is degraded, whether the live
 * feed is up, and who they are signed in as. The twin digest is shown because
 * two operators reading two screens must be able to prove they are looking at
 * the same city state.
 */

"use client";

import { useState } from "react";

import { session } from "@/lib/api";
import { ageTone, formatAgeFrom, formatClock } from "@/lib/format";
import { useConsole } from "@/lib/store";

import { Logo } from "./Logo";
import { Chip } from "./primitives";

export interface LiveSummary {
  connected: boolean;
  reconnects: number;
  snapshotAgeS: number | null;
  gapDetected: boolean;
}

export function TopBar({
  live,
  onSignOut,
  onDrill,
}: {
  live: LiveSummary;
  onSignOut(): void;
  onDrill(mmH: number): void;
}) {
  const twin = useConsole((state) => state.twin);
  const health = useConsole((state) => state.health);
  const feed = useConsole((state) => state.feed);
  const runCycle = useConsole((state) => state.runCycle);
  const toast = useConsole((state) => state.toast);
  const [cyclinging, setCycling] = useState(false);

  const ageSeconds = twin?.computed_at ? Math.max(0, (Date.now() - new Date(twin.computed_at).getTime()) / 1000) : null;
  const tone = ageTone(ageSeconds, 90, 300);
  const mode = health?.mode ?? "unknown";
  const degraded = Boolean(twin?.degraded) || mode === "degraded" || mode === "read_only";

  async function force() {
    setCycling(true);
    await runCycle();
    setCycling(false);
  }

  function drill() {
    const value = window.prompt(
      "Synthetic rainfall for the telemetry replay (mm/h). The engines treat it as observed rain. Use 0 to clear.",
      "68",
    );
    if (value === null) return;
    const parsed = Number(value);
    if (Number.isNaN(parsed) || parsed < 0) {
      toast("warn", "Not a usable rainfall figure", "Enter rainfall intensity in mm/h, for example 68.");
      return;
    }
    onDrill(parsed);
    toast("info", `Drill rainfall set to ${parsed} mm/h`, "The next cycle ingests it as observed telemetry. This is a training action and is audited.");
  }

  return (
    <header className="topbar">
      <div className="brand">
        <Logo size={30} withWordmark />
      </div>

      <div className="badges">
        <Chip tone="tone-info" title="The single city this appliance is responsible for.">
          {twin?.city.name ?? "city not loaded"}
        </Chip>
        <Chip
          tone={tone === "ok" ? "tone-ok" : tone === "warn" ? "tone-warn" : "tone-bad"}
          title={`Twin computed at ${formatClock(twin?.computed_at)}. Digest ${twin?.digest ?? "unknown"}.`}
        >
          <span className="dot" />
          twin {formatAgeFrom(twin?.computed_at)}
        </Chip>
        <Chip
          tone={feed.connected ? "tone-ok" : "tone-bad"}
          title={
            feed.connected
              ? `Live feed connected, sequence ${feed.lastSeq}, ${feed.reconnects} reconnects this session.`
              : "The live feed is down. The console is showing the last state it received and says so rather than pretending."
          }
        >
          <span className={`dot ${feed.connected ? "pulse" : ""}`} />
          {feed.connected ? "live" : "feed down"} · seq {feed.lastSeq}
        </Chip>
        {live.snapshotAgeS !== null ? (
          <Chip tone={live.snapshotAgeS > 300 ? "tone-warn" : "tone-ok"} title="Age of the API's own twin snapshot, reported on every heartbeat.">
            api snapshot {Math.round(live.snapshotAgeS)} s
          </Chip>
        ) : null}
        <Chip
          tone={degraded ? "tone-warn" : "tone-ok"}
          title={
            degraded
              ? "Degraded mode: storage or the local model is impaired. Decisions still run from the last verified state."
              : "All subsystems nominal."
          }
        >
          mode <strong>{mode}</strong>
        </Chip>
        <Chip title="Hash of the current twin snapshot. Two operators comparing digests are provably looking at the same city state.">
          digest <strong className="mono">{twin?.digest?.slice(0, 12) ?? "—"}</strong>
        </Chip>
        {twin ? (
          <Chip title="Assets, dependency edges and live sensors in the digital twin.">
            {twin.stats.nodes} assets · {twin.stats.edges} edges · {twin.sensors.length} sensors
          </Chip>
        ) : null}
        {health ? (
          <Chip
            tone={
              ((health.checks.connectors as { degraded?: unknown[] } | undefined)?.degraded?.length ?? 0) > 0 ? "tone-warn" : "tone-ok"
            }
            title="Ingest connectors and whether their last poll succeeded. A stale connector is shown, never silently skipped."
          >
            {(() => {
              const connectors = health.checks.connectors as { count?: number; degraded?: unknown[] } | undefined;
              const total = connectors?.count ?? 0;
              const stale = connectors?.degraded?.length ?? 0;
              return total ? `${total - stale} of ${total} connectors healthy` : "no connectors";
            })()}
          </Chip>
        ) : null}
      </div>

      <span className="topbar-spacer" />

      <div className="badges">
        <button className="btn small" onClick={drill} title="Drive the synthetic telemetry replay for a training drill.">
          Drill
        </button>
        <button className="btn small" onClick={force} disabled={cyclinging} title="Force one full Collect → … → Recommend pass now.">
          {cyclinging ? <span className="spin" /> : null}
          {cyclinging ? "Running cycle" : "Run cycle"}
        </button>
        <Chip tone="tone-info" title={`Signed in as ${session.identity?.subject ?? "unknown"}`}>
          {session.identity?.subject ?? "anonymous"} · {session.identity?.role}
        </Chip>
        <button className="btn small ghost" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </header>
  );
}
