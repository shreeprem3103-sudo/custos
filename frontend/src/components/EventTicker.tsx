/**
 * Live event ticker.
 *
 * The bottom strip is the console's peripheral vision: ingestion, anomalies,
 * incident changes and simulation progress scroll past so an operator does not
 * have to go looking. It is keyboard-focusable and pausable, because a ticker
 * that cannot be frozen is a ticker that distracts during a briefing.
 */

"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { formatClock, titleise } from "@/lib/format";
import { useConsole } from "@/lib/store";
import type { StreamEvent } from "@/lib/types";

import { Chip } from "./primitives";

const TONE_BY_TOPIC: Record<string, string> = {
  "system.alert": "tone-bad",
  "sensor.anomaly": "tone-warn",
  "incident.new": "tone-bad",
  "incident.update": "tone-warn",
  "simulation.complete": "tone-ok",
  "simulation.progress": "tone-info",
  "recommendation.new": "tone-info",
  "task.update": "tone-info",
  "twin.snapshot": "tone-mute",
  "sensor.reading": "tone-mute",
};

function summarise(event: StreamEvent): string {
  const payload = event.payload ?? {};
  switch (event.topic) {
    case "sensor.reading":
      return `${String(payload.gateway ?? "gateway")} · ${String(payload.accepted ?? 0)} accepted, ${String(payload.duplicates ?? 0)} deduped`;
    case "sensor.anomaly":
      return `${String(payload.sensor_id ?? "sensor")} z=${Number(payload.z ?? 0).toFixed(1)} (baseline ${Number(payload.baseline ?? 0).toFixed(2)})`;
    case "simulation.progress":
      return `${String(payload.simulation_id ?? "")} ${String(payload.status ?? "")} ${payload.progress ? `${Math.round(Number(payload.progress) * 100)}%` : ""}`;
    case "simulation.complete":
      return `${String(payload.name ?? payload.simulation_id ?? "")} in ${Math.round(Number(payload.runtime_ms ?? 0) / 1000)} s`;
    case "incident.new":
      return `sev ${String(payload.severity ?? "?")} · ${String(payload.title ?? "")}`.slice(0, 90);
    case "incident.update":
      return `${String(payload.incident_id ?? "")} · ${String(payload.action ?? "update")}`;
    case "task.update":
      return `task ${String(payload.task_id ?? "")} ${String(payload.action ?? "")}`.slice(0, 80);
    case "recommendation.new":
      return String(payload.action ?? "").slice(0, 90);
    case "twin.snapshot":
      return `digest ${String(payload.digest ?? "").slice(0, 10)}`;
    case "system.alert":
      return String(payload.message ?? payload.detail ?? "").slice(0, 90);
    default:
      return JSON.stringify(payload).slice(0, 80);
  }
}

export function EventTicker({ events }: { events: StreamEvent[] }) {
  const feed = useConsole((state) => state.feed);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState<string>("all");
  const buffer = useRef<StreamEvent[]>([]);
  const [, force] = useState(0);

  // While paused, keep collecting: the point of pausing is to *read*, so the
  // data must not be lost — it is simply not scrolled past.
  useEffect(() => {
    if (!paused) return;
    buffer.current = events;
  }, [events, paused]);

  const visible = useMemo(() => {
    const source = paused ? buffer.current : events;
    const list = filter === "all" ? source : source.filter((event) => event.topic === filter);
    return list.slice(0, 40);
  }, [events, filter, paused]);

  const topics = useMemo(() => ["all", ...new Set(events.map((event) => event.topic))], [events]);

  return (
    <div className="ticker">
      <div className="row" style={{ gap: 6 }}>
        <Chip tone={feed.connected ? "tone-ok" : "tone-bad"} title={feed.error ?? undefined}>
          <span className={`dot ${feed.connected ? "pulse" : ""}`} />
          {feed.connected ? "streaming" : "offline"}
        </Chip>
        {feed.gapDetected ? (
          <Chip tone="tone-warn" title="The console was away long enough that the server's event ring no longer had the missing events. It refetched the twin instead of hiding the hole.">
            gap repaired
          </Chip>
        ) : null}
        {feed.reconnects > 0 ? <Chip tone="tone-mute" title="Reconnects this session (network or sleep events).">{feed.reconnects} reconnects</Chip> : null}
      </div>

      <div className="ticker-events" onClick={() => setPaused((value) => !value)} title={paused ? "Click to resume scrolling" : "Click to pause so you can read"}>
        {visible.length === 0 ? (
          <span className="mute">
            {filter === "all" ? "no events yet — the feed is quiet" : `no ${filter} events yet`}
          </span>
        ) : (
          visible.map((event) => (
            <span className="ticker-event" key={`${event.seq}-${event.topic}`}>
              <span className="mute">{formatClock(event.at)}</span>
              <span className={`topic ${TONE_BY_TOPIC[event.topic] ?? ""}`}>#{event.seq}</span>
              <span className={`topic ${TONE_BY_TOPIC[event.topic] ?? ""}`}>{event.topic}</span>
              <span className="src">{event.source && event.source !== "system" ? `${event.source} ·` : ""}</span>
              <span>{summarise(event)}</span>
            </span>
          ))
        )}
      </div>

      <div className="row" style={{ gap: 6 }}>
        <select
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
            force((value) => value + 1);
          }}
          style={{ width: 168, padding: "3px 6px", fontSize: 11.5 }}
          title="Filter the ticker by event topic."
        >
          {topics.map((topic) => (
            <option key={topic} value={topic}>
              {topic === "all" ? "all topics" : titleise(topic)}
            </option>
          ))}
        </select>
        <button className="btn small ghost" onClick={() => setPaused((value) => !value)} title="Freeze the ticker without losing events.">
          {paused ? "▶ resume" : "❚❚ pause"}
        </button>
      </div>
    </div>
  );
}
