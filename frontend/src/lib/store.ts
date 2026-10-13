/**
 * Console state.
 *
 * The console is not a dashboard that renders whatever the last request
 * returned; it is a shared operational picture with several independent writers
 * (scheduled cycles, the operator's own What-If runs, the live event feed) and
 * several readers (map, panels, ticker). That is exactly the situation where
 * ad-hoc `useState` starts lying about what is current, so the state lives in
 * one typed store with an explicit update path per data source:
 *
 * * `bootstrap()`          — first load: identity, twin, stages, incidents.
 * * `refreshTwin()`        — cheap poll of the twin + stage view.
 * * `applyEvent(e)`       — the live feed; merges without a full refetch.
 * * `startSimulation()`   — the What-If run lifecycle (accepted → running → done).
 *
 * Every piece of state carries *when it was observed* and *how confident it is*,
 * because an operator must be able to tell the difference between "the city is
 * quiet" and "our last update was six minutes ago".
 */

import { create } from "zustand";

import { api, ApiError, session } from "./api";
import type {
  AiStatusOut,
  AnomalyOut,
  AssumptionsOut,
  CascadeImpact,
  CycleOut,
  HealthOut,
  IncidentOut,
  KpiDefinition,
  KpiMap,
  Recommendation,
  SimulationRequestIn,
  SimulationResult,
  SimulationSummaryOut,
  StageOut,
  StreamEvent,
  TaskOut,
  TimelineFrame,
  TriageOut,
  TwinOut,
} from "./types";

export type Panel = "whatif" | "compare" | "incidents" | "evidence";

export interface LayerState {
  hazards: boolean;
  assets: boolean;
  mobility: boolean;
  cascade: boolean;
  zones: boolean;
  spof: boolean;
  labels: boolean;
}

export interface ConsoleToast {
  id: string;
  level: "info" | "warn" | "error" | "success";
  message: string;
  detail?: string;
  at: number;
}

interface FeedState {
  connected: boolean;
  lastSeq: number;
  lastEventAt: string | null;
  droppedEvents: number;
  reconnects: number;
  gapDetected: boolean;
  error: string | null;
}

interface SimulationState {
  runId: string | null;
  status: string;
  progress: number;
  message: string;
  startedAt: number | null;
  result: SimulationResult | null;
  loading: boolean;
  error: string | null;
}

interface ConsoleState {
  // --- session -------------------------------------------------------- //
  ready: boolean;
  bootError: string | null;

  // --- live twin ------------------------------------------------------ //
  twin: TwinOut | null;
  twinAt: number | null;
  stages: StageOut[];
  stagesAt: number | null;
  cycle: CycleOut | null;
  health: HealthOut | null;
  anomalies: AnomalyOut[];
  incidents: IncidentOut[];
  tasks: TaskOut[];
  simulationHistory: SimulationSummaryOut[];
  kpis: KpiDefinition[];
  assumptions: AssumptionsOut | null;
  ai: AiStatusOut | null;
  feed: FeedState;

  // --- operator ------------------------------------------------------- //
  panel: Panel;
  selectedAssetId: string | null;
  selectedIncidentId: string | null;
  compareA: string | null;
  compareB: string | null;
  layers: LayerState;
  timelineIndex: number | null;
  scenario: SimulationRequestIn;
  simulation: SimulationState;
  toasts: ConsoleToast[];

  // --- actions -------------------------------------------------------- //
  bootstrap(): Promise<void>;
  refreshTwin(force?: boolean): Promise<void>;
  refreshOps(): Promise<void>;
  runCycle(): Promise<void>;
  applyEvent(event: StreamEvent): void;
  setFeed(patch: Partial<FeedState>): void;
  setPanel(panel: Panel): void;
  selectAsset(assetId: string | null): void;
  selectIncident(incidentId: string | null): void;
  setCompare(slot: "a" | "b", id: string | null): void;
  toggleLayer(layer: keyof LayerState): void;
  setTimelineIndex(index: number | null): void;
  updateScenario(patch: Partial<SimulationRequestIn>): void;
  resetScenario(preset?: "burst" | "monsoon" | "calm"): void;
  startSimulation(): Promise<void>;
  compareScenarios(): Promise<string | null>;
  triageIncident(incidentId: string): Promise<TriageOut | null>;
  commitRecommendation(recommendation: Recommendation, assignedTo: string | null): Promise<TaskOut | null>;
  loadSimulation(runId: string, options?: { poll?: boolean }): Promise<SimulationResult | null>;
  toast(level: ConsoleToast["level"], message: string, detail?: string): void;
  dismissToast(id: string): void;
}

const DEFAULT_SCENARIO: SimulationRequestIn = {
  name: "What-If: 60 mm/h burst over central Kochi",
  description: "Commander what-if scenario",
  horizon_minutes: 360,
  timestep_minutes: 15,
  ensemble_size: 24,
  random_seed: 7,
  hazards: [
    {
      hazard_type: "rainfall_burst",
      location: { lon: 76.296, lat: 9.978 },
      radius_m: 6000,
      intensity_mm_h: 60,
      start_min: 30,
      duration_min: 180,
      description: "convective burst over the central wards",
    },
  ],
  interventions: [],
  asset_overrides: [],
};

const PRESETS: Record<"burst" | "monsoon" | "calm", () => SimulationRequestIn> = {
  burst: () => DEFAULT_SCENARIO,
  monsoon: () => ({
    ...DEFAULT_SCENARIO,
    name: "What-If: monsoon system, 130 mm/h for 4 h + spring tide",
    description: "Extreme, but not unprecedented: the 2018 and 2019 peaks both exceeded this",
    horizon_minutes: 720,
    hazards: [
      { hazard_type: "rainfall_burst", location: { lon: 76.32, lat: 10.03 }, radius_m: 14000, intensity_mm_h: 130, start_min: 0, duration_min: 240, description: "monsoon core over Ernakulam" },
      { hazard_type: "tidal_surge", surge_m: 0.6, start_min: 0, duration_min: 720, description: "spring high water blocking gravity outfalls" },
    ],
  }),
  calm: () => ({
    ...DEFAULT_SCENARIO,
    name: "What-If: dry day, no forcing (control run)",
    description: "A zero-forcing control: every KPI must read zero. If it does not, the model is wrong.",
    hazards: [],
  }),
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

let eventLogAt = 0;

export const useConsole = create<ConsoleState>((set, get) => ({
  ready: false,
  bootError: null,

  twin: null,
  twinAt: null,
  stages: [],
  stagesAt: null,
  cycle: null,
  health: null,
  anomalies: [],
  incidents: [],
  tasks: [],
  simulationHistory: [],
  kpis: [],
  assumptions: null,
  ai: null,
  feed: { connected: false, lastSeq: 0, lastEventAt: null, droppedEvents: 0, reconnects: 0, gapDetected: false, error: null },

  panel: "whatif",
  selectedAssetId: null,
  selectedIncidentId: null,
  compareA: null,
  compareB: null,
  layers: { hazards: true, assets: true, mobility: true, cascade: true, zones: true, spof: true, labels: true },
  timelineIndex: null,
  scenario: DEFAULT_SCENARIO,
  simulation: { runId: null, status: "idle", progress: 0, message: "", startedAt: null, result: null, loading: false, error: null },
  toasts: [],

  async bootstrap() {
    try {
      const [twin, stages, incidents, kpis, ai, assumptions, health] = await Promise.all([
        api<TwinOut>("/twin?include=assets,zones,sensors,insights,fusion,prediction"),
        api<{ stages: StageOut[] }>("/stages"),
        api<{ incidents: IncidentOut[] }>("/incidents?limit=50"),
        api<{ kpis: KpiDefinition[] }>("/simulations/meta/kpis"),
        api<AiStatusOut>("/ai/status"),
        api<AssumptionsOut>("/simulations/meta/assumptions"),
        api<HealthOut>("/health"),
      ]);
      set({
        twin,
        twinAt: Date.now(),
        stages: stages.stages,
        stagesAt: Date.now(),
        incidents: incidents.incidents,
        kpis: kpis.kpis,
        ai,
        assumptions,
        health,
        ready: true,
        bootError: null,
      });
      await get().refreshOps();
      if (!ai.ollama.reachable) {
        get().toast("warn", "Local reasoning model offline", "Predictions and numbers are unaffected - every figure is computed by the engines. Only the natural-language phrasing falls back to templates.");
      }
    } catch (error) {
      set({ ready: false, bootError: errorText(error) });
    }
  },

  async refreshTwin() {
    try {
      const twin = await api<TwinOut>("/twin?include=assets,zones,sensors,insights");
      set({ twin, twinAt: Date.now() });
    } catch (error) {
      if (error instanceof ApiError && error.isAuth) return;
      get().toast("error", "Twin refresh failed", errorText(error));
    }
  },

  async refreshOps() {
    try {
      const [stages, health, anomalies, incidents, tasks, history] = await Promise.all([
        api<{ stages: StageOut[] }>("/stages"),
        api<HealthOut>("/health"),
        api<AnomalyOut[]>("/anomalies?limit=20"),
        api<{ incidents: IncidentOut[] }>("/incidents?limit=50"),
        api<{ tasks: TaskOut[] }>("/tasks?limit=50"),
        api<{ simulations: SimulationSummaryOut[] }>("/simulations?limit=25"),
      ]);
      set({
        stages: stages.stages,
        stagesAt: Date.now(),
        health,
        anomalies,
        incidents: incidents.incidents,
        tasks: tasks.tasks,
        simulationHistory: history.simulations,
      });
      // Keep the comparison slots pointing at stored runs that still exist.
      if (!get().compareA && history.simulations.length) set({ compareA: history.simulations[0]?.id ?? null });
      if (!get().compareB && (history.simulations.length ?? 0) > 1) set({ compareB: history.simulations[1]?.id ?? null });
    } catch (error) {
      if (error instanceof ApiError && error.isAuth) return;
      get().toast("error", "Operational state refresh failed", errorText(error));
    }
  },

  async runCycle() {
    try {
      const cycle = await api<CycleOut>("/cycle?force=true", { method: "POST" });
      set({ cycle });
      await get().refreshOps();
      await get().refreshTwin();
      get().toast("success", `Cycle ${cycle.cycle_id} complete in ${(cycle.duration_ms / 1000).toFixed(1)} s`, `${cycle.readings_ingested} readings ingested, ${cycle.anomalies} anomalies`);
    } catch (error) {
      get().toast("error", "Forced cycle failed", errorText(error));
    }
  },

  applyEvent(event) {
    const feed = get().feed;
    const gap = feed.lastSeq > 0 && event.seq > feed.lastSeq + 1;
    set({
      feed: {
        ...feed,
        lastSeq: Math.max(feed.lastSeq, event.seq),
        lastEventAt: event.at,
        gapDetected: feed.gapDetected || gap,
      },
    });

    switch (event.topic) {
      case "twin.snapshot": {
        const snapshot = event.payload.snapshot as TwinOut | undefined;
        if (snapshot?.assets) set({ twin: snapshot, twinAt: Date.now() });
        break;
      }
      case "simulation.progress": {
        const runId = String(event.payload.simulation_id ?? "");
        if (runId && runId === get().simulation.runId) {
          set({
            simulation: {
              ...get().simulation,
              status: String(event.payload.status ?? "running"),
              progress: Number(event.payload.progress ?? get().simulation.progress),
              message: String(event.payload.message ?? get().simulation.message),
            },
          });
        }
        break;
      }
      case "simulation.complete": {
        const runId = String(event.payload.simulation_id ?? "");
        if (runId && runId === get().simulation.runId) {
          void get().loadSimulation(runId);
        }
        break;
      }
      case "incident.new":
      case "incident.update":
      case "task.update":
      case "recommendation.new":
      case "sensor.anomaly": {
        // Coalesce: a storm produces events faster than a human reads, and
        // refreshing the panel list on every single one would melt the tab.
        const now = Date.now();
        if (now - eventLogAt > 4000) {
          eventLogAt = now;
          void get().refreshOps();
        }
        break;
      }
      case "system.status": {
        const status = String(event.payload.status ?? "normal");
        // Merge into the existing record so a live status event cannot produce a
        // health object that is "fresh but incomplete".
        const previous = get().health;
        set({ health: previous ? { ...previous, status, mode: String(event.payload.mode ?? status) } : previous });
        break;
      }
      default:
        break;
    }
  },

  setFeed(patch) {
    set({ feed: { ...get().feed, ...patch } });
  },

  setPanel(panel) {
    set({ panel });
  },
  selectAsset(assetId) {
    set({ selectedAssetId: assetId });
  },
  selectIncident(incidentId) {
    set({ selectedIncidentId: incidentId });
  },
  setCompare(slot, id) {
    set(slot === "a" ? { compareA: id } : { compareB: id });
  },
  toggleLayer(layer) {
    set({ layers: { ...get().layers, [layer]: !get().layers[layer] } });
  },
  setTimelineIndex(index) {
    set({ timelineIndex: index });
  },

  updateScenario(patch) {
    set({ scenario: { ...get().scenario, ...patch } });
  },

  resetScenario(preset = "burst") {
    set({ scenario: PRESETS[preset]() });
  },

  async startSimulation() {
    const scenario = get().scenario;
    set({
      simulation: { runId: null, status: "submitting", progress: 0, message: "accepted by the simulation service", startedAt: Date.now(), result: null, loading: true, error: null },
    });
    try {
      const accepted = await api<{ id: string; status: string; determinism_token?: string }>("/simulations", { method: "POST", body: scenario });
      set({
        simulation: { ...get().simulation, runId: accepted.id, status: accepted.status, message: "running the ensemble", progress: 0.02, startedAt: Date.now() },
      });
      await get().loadSimulation(accepted.id, { poll: true });
      // keep the comparison slots useful: a new run becomes scenario B
      set({ compareA: get().compareA ?? accepted.id, compareB: get().compareB ?? accepted.id });
    } catch (error) {
      set({ simulation: { ...get().simulation, loading: false, status: "failed", error: errorText(error), message: errorText(error) } });
      get().toast("error", "What-If run failed", errorText(error));
    }
  },

  async loadSimulation(runId, options: { poll?: boolean } = {}) {
    const deadline = Date.now() + 10 * 60_000;
    let attempt = 0;
    /* eslint-disable no-await-in-loop */
    while (true) {
      attempt += 1;
      try {
        const result = await api<SimulationResult>(`/simulations/${runId}`);
        set({
          simulation: { ...get().simulation, runId, result, loading: false, status: "complete", progress: 1, message: "complete", error: null },
          timelineIndex: null,
        });
        void get().refreshOps();
        return result;
      } catch (error) {
        const status = error instanceof ApiError ? error.status : 0;
        // 202 (still running) and 404 (not finished being written) both mean "wait";
        // anything else is a real failure and should be said out loud.
        const pending = status === 202 || status === 404 || status === 0;
        if (!options.poll || !pending || Date.now() > deadline) {
          if (pending && options.poll) {
            set({ simulation: { ...get().simulation, loading: false, status: "timeout", message: "the ensemble is taking longer than expected; the run continues server-side and will appear in the run history" } });
            get().toast("warn", "What-If run still running", "The simulation continues on the server. It will appear in the run history when it completes.");
            void get().refreshOps();
            return null;
          }
          set({ simulation: { ...get().simulation, loading: false, status: "failed", error: errorText(error) } });
          get().toast("error", "Could not load the run", errorText(error));
          return null;
        }
        const statusView = await api<{ status: string; progress: number; message: string }>(`/simulations/${runId}/status`).catch(() => null);
        set({
          simulation: {
            ...get().simulation,
            status: statusView?.status ?? "running",
            progress: statusView?.progress ?? Math.min(0.95, 0.05 * attempt),
            message: statusView?.message ?? "running the ensemble",
          },
        });
        await new Promise((resolve) => setTimeout(resolve, Math.min(3000, 400 + attempt * 250)));
      }
    }
    /* eslint-enable no-await-in-loop */
  },

  async compareScenarios() {
    const { compareA, compareB } = get();
    if (!compareA || !compareB) {
      get().toast("warn", "Pick two runs to compare", "Scenario A and scenario B must both be stored runs.");
      return null;
    }
    try {
      const comparison = await api<{ verdict: string }>("/simulations/compare", {
        method: "POST",
        body: { scenario_a_id: compareA, scenario_b_id: compareB, weights: { patients_at_risk: 2, hospital_capacity_lost_pct: 1.5, shelters_over_capacity_people: 1.2 } },
      });
      return comparison.verdict;
    } catch (error) {
      get().toast("error", "Comparison failed", errorText(error));
      return null;
    }
  },

  async triageIncident(incidentId) {
    try {
      const triage = await api<TriageOut>(`/incidents/${incidentId}/triage`, { method: "POST" });
      await get().refreshOps();
      get().toast("success", `Triage: ${triage.decision}`, triage.rationale.slice(0, 160));
      return triage;
    } catch (error) {
      get().toast("error", "Triage failed", errorText(error));
      return null;
    }
  },

  async commitRecommendation(recommendation, assignedTo) {
    const { simulation } = get();
    try {
      const task = await api<TaskOut>(`/simulations/${simulation.runId}/recommendations/${recommendation.id}/commit`, {
        method: "POST",
        body: { assigned_to: assignedTo, priority: Math.max(1, 6 - recommendation.urgency) },
      });
      await get().refreshOps();
      get().toast("success", `Committed: ${task.title.slice(0, 70)}`, `Task ${task.id} is ${task.status}${task.assigned_to ? `, assigned to ${task.assigned_to}` : ""}`);
      return task;
    } catch (error) {
      get().toast("error", "Commit failed", errorText(error));
      return null;
    }
  },

  toast(level, message, detail) {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const toast: ConsoleToast = { id, level, message, detail, at: Date.now() };
    set({ toasts: [toast, ...get().toasts].slice(0, 4) });
    const ttl = level === "error" ? 12_000 : 7_000;
    setTimeout(() => get().dismissToast(id), ttl);
  },

  dismissToast(id) {
    set({ toasts: get().toasts.filter((toast) => toast.id !== id) });
  },
}));

/** Derived: the timeline frame the map should render, or `null` for "now". */
export function frameFor(state: Pick<ConsoleState, "simulation" | "timelineIndex">): TimelineFrame | null {
  const frames = state.simulation.result?.timeline ?? [];
  if (!frames.length || state.timelineIndex === null) return null;
  return frames[Math.min(state.timelineIndex, frames.length - 1)] ?? null;
}

/** Derived: cascade impacts at the scrubbed timestep (all of them when live). */
export function cascadeAt(result: SimulationResult | null, timelineIndex: number | null): CascadeImpact[] {
  if (!result) return [];
  if (timelineIndex === null) return result.cascade;
  const frames = result.timeline;
  const frame = frames[Math.min(timelineIndex, frames.length - 1)];
  if (!frame) return result.cascade;
  const window = result.request.timestep_minutes || 15;
  return result.cascade.filter((impact) => impact.timestep_min >= frame.t_min && impact.timestep_min < frame.t_min + window);
}

/** Derived: KPI values for a frame (falls back to the headline KPIs). */
export function kpisFor(result: SimulationResult | null, timelineIndex: number | null): KpiMap {
  if (!result) return {};
  const frame = timelineIndex === null ? null : result.timeline[Math.min(timelineIndex, result.timeline.length - 1)];
  return frame ? frame.kpis : result.kpis;
}

export { errorText };
