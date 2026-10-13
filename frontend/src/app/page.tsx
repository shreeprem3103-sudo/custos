/**
 * The command console shell.
 *
 * Owns the three lifecycles that must not be entangled: the session, the polling
 * of the twin (cheap, regular) and the live event feed (fast, bursty). The panels
 * below read from the store, so no panel can start its own polling loop and
 * quietly disagree with another one about the city.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ComparePanel } from "@/components/ComparePanel";
import { EventTicker } from "@/components/EventTicker";
import { EvidencePanel } from "@/components/EvidencePanel";
import { IncidentsPanel } from "@/components/IncidentsPanel";
import { LeftRail } from "@/components/LeftRail";
import { LoginScreen } from "@/components/LoginScreen";
import { MapCanvas } from "@/components/MapCanvas";
import { Toasts } from "@/components/Toasts";
import { TopBar, type LiveSummary } from "@/components/TopBar";
import { WhatIfPanel } from "@/components/WhatIfPanel";
import { Chip } from "@/components/primitives";
import { session } from "@/lib/api";
import { LiveFeed } from "@/lib/live";
import { useConsole, type Panel as PanelName } from "@/lib/store";
import type { StreamEvent } from "@/lib/types";

const TWIN_POLL_MS = 20_000;
const OPS_POLL_MS = 60_000;
const FEED_BUFFER = 200;

export default function ConsolePage() {
  const [signedIn, setSignedIn] = useState(false);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [live, setLive] = useState<LiveSummary>({ connected: false, reconnects: 0, snapshotAgeS: null, gapDetected: false });

  const ready = useConsole((state) => state.ready);
  const bootError = useConsole((state) => state.bootError);
  const panel = useConsole((state) => state.panel);
  const setPanel = useConsole((state) => state.setPanel);
  const bootstrap = useConsole((state) => state.bootstrap);
  const refreshTwin = useConsole((state) => state.refreshTwin);
  const refreshOps = useConsole((state) => state.refreshOps);
  const applyEvent = useConsole((state) => state.applyEvent);
  const toast = useConsole((state) => state.toast);
  const health = useConsole((state) => state.health);
  const feedRef = useRef<LiveFeed | null>(null);
  const eventsRef = useRef<StreamEvent[]>([]);

  useEffect(() => {
    setSignedIn(session.isAuthenticated);
  }, []);

  /* ----------------------------------------------------------- data load */
  useEffect(() => {
    if (!signedIn) return;
    void bootstrap();
  }, [signedIn, bootstrap]);

  useEffect(() => {
    if (!signedIn || !ready) return;
    const twinTimer = setInterval(() => void refreshTwin(), TWIN_POLL_MS);
    const opsTimer = setInterval(() => void refreshOps(), OPS_POLL_MS);
    return () => {
      clearInterval(twinTimer);
      clearInterval(opsTimer);
    };
  }, [signedIn, ready, refreshTwin, refreshOps]);

  /* ------------------------------------------------------------ live feed */
  useEffect(() => {
    if (!signedIn || !ready) return;
    const feed = new LiveFeed({
      onEvent: (event) => {
        applyEvent(event);
        eventsRef.current = [event, ...eventsRef.current].slice(0, FEED_BUFFER);
        setEvents(eventsRef.current);
      },
      onState: (patch) => {
        useConsole.getState().setFeed(patch);
        setLive((previous) => ({
          connected: patch.connected ?? previous.connected,
          reconnects: patch.reconnects ?? previous.reconnects,
          snapshotAgeS: patch.snapshotAgeS ?? previous.snapshotAgeS,
          gapDetected: patch.gapDetected ?? previous.gapDetected,
        }));
      },
      onResync: (reason) => {
        // The console repaired itself rather than hiding a hole in the record.
        toast("warn", "Live feed resumed after a gap", `${reason}. The console refetched the twin so nothing between the two is silently missing.`);
        void refreshTwin();
        void refreshOps();
      },
    });
    feedRef.current = feed;
    feed.connect(0);
    return () => {
      feed.disconnect();
      feedRef.current = null;
    };
  }, [signedIn, ready, applyEvent, refreshTwin, refreshOps, toast]);

  const handleSignOut = useCallback(() => {
    feedRef.current?.disconnect();
    eventsRef.current = [];
    setEvents([]);
    setSignedIn(false);
  }, []);

  if (!signedIn) {
    return (
      <>
        <LoginScreen
          onSignedIn={() => {
            setSignedIn(true);
          }}
        />
        <Toasts />
      </>
    );
  }

  const degraded = health?.mode === "degraded" || health?.mode === "read_only";

  return (
    <div className="app">
      <TopBar live={live} onSignOut={handleSignOut} onDrill={(mmH) => feedRef.current?.setDrillRainfall(mmH)} />

      {bootError ? (
        <div className="bad-banner">
          <strong>Cannot load the city state.</strong> {bootError}
          <span className="grow" />
          <button className="btn small" onClick={() => void bootstrap()}>
            retry
          </button>
        </div>
      ) : null}

      {degraded && !bootError ? (
        <div className="warn-banner">
          <strong>Degraded operation.</strong> The appliance is running in <span className="mono">{health?.mode}</span> mode — ingest or the
          local model is impaired. Decisions continue from the last verified state, and every panel states the age of what it is showing.
        </div>
      ) : null}

      <main className="main">
        <LeftRail />

        <div className="column">
          <MapCanvas />
        </div>

        <section className="panel" style={{ minHeight: 0 }}>
          <nav className="tabs">
            {(
              [
                ["whatif", "What-If"],
                ["compare", "Compare"],
                ["incidents", `Incidents`],
                ["evidence", "Evidence"],
              ] as [PanelName, string][]
            ).map(([id, label]) => (
              <button key={id} className={`tab ${panel === id ? "active" : ""}`} onClick={() => setPanel(id)}>
                {label}
              </button>
            ))}
            <span className="grow" />
            <Chip tone="tone-mute">one city · one appliance</Chip>
          </nav>
          <div className="scroll" style={{ flex: 1, padding: 10, display: "flex", flexDirection: "column", gap: 10 }}>
            {panel === "whatif" ? <WhatIfPanel /> : null}
            {panel === "compare" ? <ComparePanel /> : null}
            {panel === "incidents" ? <IncidentsPanel /> : null}
            {panel === "evidence" ? <EvidencePanel /> : null}
          </div>
        </section>
      </main>

      <EventTicker events={events} />
      <Toasts />
    </div>
  );
}
