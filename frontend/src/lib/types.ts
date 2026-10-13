/**
 * CUSTOS console — API contract.
 *
 * These types mirror the Pydantic models in `backend/custos/domain/schemas.py`.
 * They are written by hand rather than generated because the console must *fail
 * loudly* when the contract moves: `npm run typecheck` is the drift alarm, and a
 * console that silently renders `undefined` during a flood is worse than one
 * that refuses to build.
 */

export type Iso = string;
export type Status = "normal" | "degraded" | "read_only" | "critical" | "ok";
export type StageState = "active" | "degraded" | "error" | "idle" | "running";
export type Band = "low" | "medium" | "high";
export type Role = "viewer" | "analyst" | "commander" | "admin";

export interface Identity {
  subject: string;
  role: Role;
  permissions: string[];
  expires_at?: Iso | null;
  auth?: string;
}

export interface AuthToken {
  access_token: string;
  refresh_token: string | null;
  token_type: string;
  expires_in: number;
  subject: string;
  role: Role;
  permissions: string[];
}

export interface GeoPoint {
  lon: number;
  lat: number;
}

export interface AssetOut {
  id: string;
  name: string;
  asset_type: string;
  subtype: string | null;
  criticality: number;
  elevation_m: number | null;
  geometry: Record<string, unknown>;
  centroid: GeoPoint | null;
  operator: string | null;
  zone: string | null;
  capacity: Record<string, unknown>;
  attributes: Record<string, unknown>;
  status: string;
  flood_depth_m: number;
  power_available: boolean;
  access_available: boolean;
  service_level: number;
  exposure_score: number;
  provenance: Record<string, unknown> | null;
  updated_at: Iso | null;
}

export interface ZoneOut {
  id: string;
  name: string;
  centroid: GeoPoint;
  population: number;
  vulnerability_index: number;
  children_pct: number;
  elderly_pct: number;
  population_density: number;
  mean_elevation_m: number | null;
  flood_depth_m: number;
  assets: number;
}

export interface SensorOut {
  id: string;
  name: string;
  metric: string;
  unit: string;
  asset_id: string | null;
  zone: string | null;
  centroid: GeoPoint | null;
  last_value: number | null;
  last_observed_at: Iso | null;
  age_seconds: number | null;
  is_active: boolean;
  baseline_median: number | null;
}

export interface GraphNodeOut {
  id: string;
  name: string;
  asset_type: string;
  status: string;
  criticality: number;
  centroid: GeoPoint;
  degree: number;
  betweenness: number;
  single_point_of_failure: boolean;
  zone: string | null;
}

export interface GraphEdgeOut {
  id: string;
  source_id: string;
  target_id: string;
  kind: string;
  criticality: number;
  weight: number;
  lead_time_min: number;
  failure_mode: string;
  recovery_hours: number;
  redundancy_group: string | null;
  attributes: Record<string, unknown>;
  geometry: Record<string, unknown> | null;
}

export interface GraphOut {
  nodes: GraphNodeOut[];
  edges: GraphEdgeOut[];
  computed_at: Iso;
  cascade_paths_sample: string[][];
}

export interface SpofEntry {
  asset_id: string;
  name: string;
  asset_type: string;
  criticality: number;
  reach: number;
  dependents: number;
  betweenness: number;
  zone: string | null;
  sole_provider_for: string[];
  rationale: string;
}

export interface Insights {
  computed_at_digest: string;
  single_points_of_failure: SpofEntry[];
  articulation_points: string[];
  bridge_edges: string[];
  betweenness: { asset_id: string; score: number }[];
  reach: Record<string, number>;
  cascade_depth_by_node: Record<string, number>;
  top_paths: string[][];
  method: string;
}

export interface TwinStats {
  nodes: number;
  edges: number;
  zones: number;
  sensors: number;
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  computed_at: Iso;
  digest: string;
  sources: string[];
  degraded: boolean;
}

export interface TwinOut {
  city: { id: string; name: string };
  computed_at: Iso;
  digest: string;
  degraded: boolean;
  sources: SensorOut[];
  stats: TwinStats;
  assets: AssetOut[];
  zones: ZoneOut[];
  sensors: SensorOut[];
  insights: Insights;
  fusion: Record<string, unknown>;
  prediction: Record<string, unknown>;
  stage_outputs: Record<string, unknown>;
}

export interface StageOut {
  stage: string;
  state: StageState;
  last_run_at: Iso | null;
  detail: string;
  throughput: number;
}

export interface StagesOut {
  stages: StageOut[];
  order: string[];
  last_cycle_at: Iso | null;
  cycle_id: string | null;
  cycles: number;
  prediction: PredictionOut;
  anomalies: number;
  origin: string;
}

export interface PredictionOut {
  horizon_minutes?: number;
  rainfall_mm_h?: number;
  observed_rainfall_mm_h?: number;
  trend?: string;
  stage_median_m?: number;
  rising_water_bodies?: string[];
  confidence?: number;
  band?: Band;
  basis?: string;
  single_points_of_failure?: string[];
  degraded?: boolean;
}

export interface CycleOut {
  cycle_id: string;
  duration_ms: number;
  degraded: boolean;
  readings_ingested: number;
  anomalies: number;
  stages: StageOut[];
}

export interface IncidentTimelineEntry {
  at: Iso;
  stage: string;
  actor: string;
  detail: string;
  evidence_ids: string[];
}

export interface IncidentAssetLink {
  asset_id: string;
  role: string;
  impact: number;
  asset_name?: string | null;
}

export interface IncidentOut {
  id: string;
  title: string;
  category: string;
  severity: number;
  status: string;
  confidence: number;
  summary: string;
  lon: number;
  lat: number;
  radius_m: number;
  zone: string | null;
  population_exposed: number;
  reported_by: string;
  detected_at: Iso;
  resolved_at: Iso | null;
  explainability: Record<string, unknown>;
  attributes: Record<string, unknown>;
  assets?: IncidentAssetLink[];
  timeline?: IncidentTimelineEntry[];
}

export interface TriageOut {
  incident_id: string;
  decision: string;
  severity_suggestion: number;
  rationale: string;
  actions: string[];
  evidence_ids: string[];
  confidence: number;
  triaged_at: Iso;
  narration?: string | null;
}

export type KpiMap = Record<string, number>;

export interface TimelineFrame {
  t_min: number;
  label: string;
  kpis: KpiMap;
  active_incidents: number;
  flood_extent_km2: number;
  max_depth_m: number;
  critical_failures: string[];
}

export interface CascadeImpact {
  timestep_min: number;
  cause_asset_id: string;
  cause_asset_name: string;
  target_asset_id: string;
  target_asset_name: string;
  target_type: string;
  mechanism: string;
  relation_kind: string | null;
  lead_time_min: number;
  magnitude: number;
  probability: number;
  status_after: string;
  hop: number;
  evidence_ids: string[];
  narrative: string;
}

export interface Recommendation {
  id: string;
  rank: number;
  action: string;
  intervention_type: string;
  target_id: string | null;
  target_name: string | null;
  window: string;
  rationale: string;
  expected_kpi_delta: KpiMap;
  resources_required: Record<string, number>;
  feasibility: number;
  urgency: number;
  confidence: number;
  evidence_ids: string[];
  counterfactual: string;
  generated_by: string;
  assumptions: string[];
}

export interface Explainability {
  confidence: number;
  band: Band;
  model: string;
  method: string;
  data_freshness: string;
  assumption_list: string[];
  evidence_ids: string[];
  uncertainty: Record<string, unknown>;
  counterfactuals: string[];
  grounded: boolean;
  notes: string[];
}

export interface SimulationRequestIn {
  name: string;
  description?: string;
  horizon_minutes: number;
  timestep_minutes: number;
  ensemble_size: number;
  random_seed?: number;
  hazards?: HazardSpec[];
  interventions?: InterventionSpec[];
  asset_overrides?: AssetOverride[];
  baseline_simulation_id?: string | null;
  tags?: string[];
  notes?: Record<string, unknown>;
}

export interface HazardSpec {
  hazard_type: string;
  location?: GeoPoint | null;
  zone_id?: string | null;
  asset_id?: string | null;
  radius_m?: number;
  intensity_mm_h?: number | null;
  surge_m?: number | null;
  start_min?: number;
  duration_min?: number;
  peak_depth_m?: number | null;
  description?: string;
}

export interface InterventionSpec {
  intervention_type: string;
  zone_id?: string | null;
  asset_id?: string | null;
  quantity?: number | null;
  start_min?: number;
  duration_min?: number | null;
  capacity_m3s?: number | null;
  capacity_people?: number | null;
  description?: string;
  parameters?: Record<string, unknown>;
}

export interface AssetOverride {
  asset_id: string;
  status?: string | null;
  flood_depth_m?: number | null;
  power_available?: boolean | null;
  access_available?: boolean | null;
  service_level?: number | null;
  note?: string;
}

export interface SimulationResult {
  id: string;
  request: SimulationRequestIn;
  status: string;
  created_at: Iso;
  completed_at: Iso | null;
  runtime_ms: number;
  kpis: KpiMap;
  uncertainty: { p10: KpiMap; p50: KpiMap; p90: KpiMap };
  timeline: TimelineFrame[];
  cascade: CascadeImpact[];
  roads: Record<string, unknown>;
  facilities: Record<string, unknown>;
  shelters: Record<string, unknown>;
  mobility: {
    avg_response_delay_min: number;
    p90_response_delay_min: number;
    severed_response_pairs: number;
    impassable_roads: string[];
    isolation_events: number;
  };
  recommendations: Recommendation[];
  sensitivity: unknown[];
  explainability: Explainability;
  baseline_id: string | null;
  baseline_kpis: KpiMap | null;
  delta: KpiMap | null;
  narrative: string;
  determinism_token: string;
  degraded: boolean;
}

export interface SimulationSummaryOut {
  id: string;
  name: string;
  description: string;
  status: string;
  created_at: Iso;
  completed_at: Iso | null;
  ensemble_size: number;
  runtime_ms: number;
  determinism_token: string;
  degraded: boolean;
  baseline_id: string | null;
  kpis: KpiMap;
  recommendation_count: number;
  cascade_edges: number;
  headline?: string;
}

export interface SimulationStatusOut {
  id: string;
  status: string;
  progress: number;
  message: string;
  started_at: Iso | null;
  finished_at: Iso | null;
  result_id: string | null;
  runtime_ms: number;
  degraded: boolean;
}

export interface ComparisonRow {
  kpi: string;
  label: string;
  unit: string;
  band_a: [number, number];
  band_b: [number, number];
  value_a: number;
  value_b: number;
  delta: number;
  delta_pct: number;
  material: boolean;
  lower_is_better: boolean;
  better: "a" | "b" | "neither";
}

export interface ComparisonOut {
  scenario_a: { id: string; name: string };
  scenario_b: { id: string; name: string };
  rows: ComparisonRow[];
  verdict: string;
  weighted_score_a: number;
  weighted_score_b: number;
  material_count: number;
  method: string;
}

export interface KpiDefinition {
  id: string;
  label: string;
  unit: string;
  direction: string;
  lower_is_better: boolean;
  category: string;
  description: string;
}

export interface AiStatusOut {
  enabled: boolean;
  policy: {
    grounding_required: boolean;
    allow_number_invention: boolean;
    fallback: string;
    redaction: string;
    audit: string;
    [key: string]: unknown;
  };
  ollama: {
    base_url: string;
    model: string;
    reachable: boolean;
    last_error?: string | null;
    circuit_open: boolean;
    probes?: number;
    fallbacks?: number;
    [key: string]: unknown;
  };
  reasoner: Record<string, unknown>;
}

export interface HealthOut {
  status: string;
  mode: string;
  version: string;
  uptime_s: number;
  city: string;
  checks: Record<string, Record<string, unknown>>;
}

export interface AnomalyOut {
  id: string;
  sensor_id: string;
  sensor_name: string;
  metric: string;
  value: number;
  baseline: number;
  sigma: number;
  z: number;
  at: Iso;
  severity: string;
  asset_id: string | null;
  zone: string | null;
  acknowledged: boolean;
  explanation?: string | null;
}

export interface TaskOut {
  id: string;
  title: string;
  description: string;
  status: string;
  priority: number;
  due_at: Iso | null;
  incident_id: string | null;
  simulation_id: string | null;
  recommendation_id: string | null;
  assigned_to: string | null;
  target_asset_id: string | null;
  resource_requirements: Record<string, unknown>;
  created_by: string;
  created_at: Iso;
  updated_at: Iso;
  history: { at: Iso; actor: string; event: string }[];
}

export interface AuditEntryOut {
  seq: number;
  at: Iso;
  actor: string;
  role: string;
  action: string;
  resource: string;
  outcome: string;
  detail: Record<string, unknown>;
  hash: string;
  prev_hash: string;
  correlation_id: string;
}

export interface AuditVerifyOut {
  valid: boolean;
  entries_checked: number;
  head: string;
  broken_at_seq?: number;
  note?: string;
}

export interface AssumptionsOut {
  hydrology: string[];
  cascade: string[];
  exposure: string[];
  interventions: string[];
  simulation: string[];
  [key: string]: string[];
}

export interface StreamEvent {
  seq: number;
  kind: string;
  topic: string;
  at: Iso;
  payload: Record<string, unknown>;
  correlation_id: string;
  source?: string;
}
