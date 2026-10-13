# CUSTOS — Crisis Digital Twin

**Predictive resilience for urban flood response.** One city, one appliance, six stages:
**Collect → Fuse → Understand → Predict → Simulate → Recommend.**

CUSTOS is not a dashboard. It is a decision instrument for an Emergency Operations
Centre: it models the water *before* it arrives, computes the dependency cascades
that turn a flooded road into a blacked-out hospital, tests a commander's what-if
against a coupled ensemble, and justifies every recommendation with the
counterfactual run that produced it. Every number on the screen is computed by a
deterministic engine; the local language model is allowed to *phrase*, never to
*invent*.

This is the MVP: the city is **Kochi (Ernakulam)** — 117 assets, 464 dependency
edges, 40 sensors, 20 wards — and it is deliberately single-city.

---

## 30-second tour

| | |
| --- | --- |
| **Collect** | Connectors normalise rain gauges, canal stage, street depth, utility SCADA and telephone reports into UTC / canonical units / WGS 84, with a `dedupe_key` that makes a store-and-forward replay a no-op. |
| **Fuse** | Robust baselines (median + 1.4826·MAD) instead of mean/σ, so a flood peak cannot mask the anomaly it belongs to. Snapshot carries a digest; a storage failure forks the last good state and says so. |
| **Understand** | A 464-edge dependency graph computes transmission (`criticality × attenuation × redundancy`), single points of failure, betweenness and two-hop reach — in milliseconds. |
| **Predict** | Lumped hydrology per water body with canal + backwater + tide, reporting a 30-minute rainfall forecast with confidence and its basis. |
| **Simulate** | A 24–48 member Monte-Carlo ensemble produces 16 KPIs with p10/p50/p90 bands, a minute-by-minute timeline, a cascade of explainable impact edges, and mobility routing where flooded roads are *severed*, not merely slowed. |
| **Recommend** | Nine candidate levers are ranked by measured effect; each recommendation is re-simulated against an equal-size, same-seed control run, and states honestly when it has no measurable effect. |

---

## Quickstart

### Windows (the supported operator path)

```bat
git clone <this repo> custos
cd custos
scripts\setup_windows.bat        :: isolated venv, deps, secrets, console build
scripts\start_custos.bat         :: starts API + engines, then opens the console
```

Then sign in with **`commander.rao` / `custos-demo`** (change it in
`backend\.env.dev` before any real deployment — the file is generated locally and
never committed).

| Script | Purpose |
| --- | --- |
| `scripts\setup_windows.bat` | One-time setup: isolated venv, backend deps, fresh secrets, `npm install`, console build |
| `scripts\start_custos.bat` | Boots API + engines, waits for *readiness* (not just an open port), builds the console if needed, opens it |
| `scripts\start_backend.bat` | API only (optionally a custom port, optionally `dev` hot reload) |
| `scripts\start_frontend.bat` | Console: `serve` (static bundle via the API) or `dev` (Next dev server on 3000) |
| `scripts\stop_custos.bat` | Stops the windows this suite opens and frees the ports |
| `scripts\smoke_windows.bat` | End-to-end verification against a running instance |

### Linux / macOS (development)

```bash
cd backend
./run_dev.sh              # creates .venv, generates .env.dev, starts the API on :8000
# console: http://127.0.0.1:8000/console/   (build it with: cd ../frontend && npm install && npm run build)
```

### Verify it actually works

```bash
python backend/scripts/smoke.py --base-url http://127.0.0.1:8000 --ensemble 12
```

It authenticates, reads the twin, forces a full cycle, launches a what-if
ensemble, waits for it, reads the recommendations, commits one to a task, compares
two runs, proves a fabricated figure is rejected by the grounding verifier, and
verifies the audit hash chain. A smoke test that only checks HTTP 200 tells an EOC
nothing.

---

## The command console

Served by the API itself at `/console` — one process, one port, no CORS, no CDN,
no tile server. It works with the network cut, which is the point.

* **Operational map (WebGL / deck.gl)** — ward cells sized by real population
  density, flood-depth shading, the road network, assets coloured by state, the
  modelled cascade as arcs, single-point-of-failure halos, and hazard footprints.
  Click anything to interrogate it; the legend states that ward geometry is
  schematic.
* **What-If engine** — state a scenario (rainfall intensity, duration, storm
  centre, storm surge), add interventions, run the ensemble, then step through it
  on the timeline scrubber. Every headline figure carries its p10–p90 band; every
  recommendation carries the measured counterfactual and can be committed to the
  field (task + resource reservation + audit entry).
* **Scenario comparison** — A/B on any two stored runs, KPI by KPI, with a
  `material` flag driven by the combined ensemble spread. Differences inside the
  noise are labelled as noise, in the table.
* **Incidents** — the board, the evidence behind each claim, linked assets, the
  decision timeline, and deterministic triage (the ladder is severity-driven; the
  model only phrases the rationale).
* **Evidence** — the selected asset's reading *and its age*, its dependency edges,
  the sensors and anomalies in scope, the run's assumption register, the AI policy
  (including "local model offline"), and on-demand verification of the audit chain.

Explainability is not a tab: uncertainty, freshness and confidence accompany the
numbers everywhere they appear.

---

## Architecture

The full diagram set (end-to-end flowchart, cycle sequence diagram, ER diagram,
dependency-graph example, resilience state machine, deployment topology) is in
[`docs/architecture.md`](docs/architecture.md). Operational procedures are in
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

```
backend/
  custos/
    api/            FastAPI routers: auth, twin, incidents, simulations, ops, ai, ws
    domain/         Pydantic contracts, enums, the twin dataclasses, KPI dictionary
    engines/        dependency graph · hydrology · simulation ensemble · KPIs · recommender
    fusion/         normalisation, the Kochi dataset, the repository (async SQLAlchemy)
    infrastructure/ models (26 tables), async engine, connection pool, health
    realtime/       event bus (bounded queues, seq replay, heartbeats)
    services/       pipeline orchestrator, connectors, local-AI reasoner
  schema/           GENERATED PostgreSQL DDL + Prisma schema (drift-checked in CI)
  scripts/          bootstrap_env · export_schema · smoke
frontend/           Next.js 15 console (static export, deck.gl map)
scripts/            Windows batch: setup / start / stop / smoke
docs/               architecture · runbook
```

### Backend

* **Async FastAPI** throughout: async SQLAlchemy 2 over `psycopg` (PostgreSQL in
  production, `aiosqlite` for a laptop), async engine pool with retry/backoff.
* **Event bus** with bounded drop-oldest subscriber queues, a 2048-event ring with
  sequence replay, and 15-second heartbeats — a dead tab cannot slow the city down.
* **LangGraph** async checkpoints mirror the multi-step reasoning state to storage,
  so an investigation survives a restart and is replayable.
* **Grounding verifier**: every numeral in model output must be a *rounded form* of
  a value the engines computed. Rejected drafts are persisted with the prompt digest;
  the response falls back to deterministic phrasing. The model can never introduce a
  number — enforced by tests, not by convention.
* **Audit chain**: append-only sha256 over `prev_hash | seq | at | actor | action |
  resource | outcome | detail`, sequence assigned in-process under a row lock. Editing
  any field is detectable at an exact `seq`.

### Frontend

Next.js 15 (App Router, static export), TypeScript strict, deck.gl for the twin,
Zustand for the shared operational state — one typed store with an explicit update
path per data source (bootstrap, poll, live event, operator action), so no panel can
quietly disagree with another about the city.

---

## API surface (abridged)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/auth/login` · `/refresh` | JWT HS256, per-user salted roster, lockout after 8 failures |
| `GET` | `/api/v1/twin` · `/twin/graph` · `/twin/assets` · `/twin/assets/{id}` | The digital twin, its graph, its assets |
| `PATCH` | `/api/v1/twin/assets/{id}/state` | Record an observed asset state (provenance-stamped) |
| `POST` | `/api/v1/cycle?force=true` | Run one full Collect → … → Recommend pass now |
| `POST` | `/api/v1/simulations` | Launch a what-if ensemble (202 + run id) |
| `GET` | `/api/v1/simulations/{id}` · `/{id}/status` · `/{id}/sensitivity` | Run result, progress, sensitivity |
| `POST` | `/api/v1/simulations/compare` | A/B comparison with material-difference flags |
| `POST` | `/api/v1/simulations/{id}/recommendations/{rec}/commit` | Commit to field: task + resources + audit |
| `GET` | `/api/v1/incidents` · `/{id}` | Incident board and detail (timeline, asset links) |
| `POST` | `/api/v1/incidents` · `/{id}/triage` · `/{id}/updates` | Log, triage, update |
| `POST` | `/api/v1/ingest/readings` · `/ingest/observation` | Gateway batch (dedupe-safe) and operator report |
| `GET` | `/api/v1/health` · `/ready` · `/stages` · `/metrics` | Health, readiness, pipeline state, metrics |
| `GET` | `/api/v1/audit` · `/audit/verify` | Audit trail and hash-chain verification |
| `GET` | `/api/v1/ai/status` · `/ai/explain/{kpi}` · `/ai/workflow` | Local-model status, KPI explanation, checkpointed investigation |
| `WS` | `/api/v1/stream` | Live feed: seq replay, heartbeats, topic filters |

Full schema: `http://127.0.0.1:8000/api/docs`.

---

## Database

`backend/schema/custos_schema.sql` (PostgreSQL 14+) and `backend/schema/schema.prisma`
are **generated from the ORM models** and checked in CI:

```bash
python backend/scripts/export_schema.py           # regenerate
python backend/scripts/export_schema.py --check   # fail on drift
```

26 tables. The ones that carry the product:

* `assets` · `relationships` · `zones` · `sensors` · `sensor_readings` — the twin,
  with a UNIQUE `dedupe_key` on readings so a field gateway's retry cannot
  double-count a flood.
* `simulations` — the request, KPIs, p10/p50/p90 bands, timeline, cascade,
  recommendations, explainability, determinism token and runtime. A run is
  reproducible from its row alone.
* `cascade_edges` · `recommendations` · `tasks` · `resource_assignments` — the
  decision trail, from modelled impact to committed action.
* `audit_log` — the hash chain. `events` — the mirrored bus, for after-action replay.

Connect to PostgreSQL by changing one line in `backend/.env.dev`:

```
CUSTOS_DATABASE_URL=postgresql+psycopg://custos:…@127.0.0.1:5432/custos
```

---

## Security & resilience

| Concern | Handling |
| --- | --- |
| Credentials | Never in git. `bootstrap_env.py` generates a 64-hex JWT secret and per-operator **salted** hashes; `.env.dev` is git-ignored, `.env.dev.example` ships with deliberately invalid placeholders |
| AuthN/Z | JWT (HS256, `aud=custos-ui`) + API keys for ingest (compared with `hmac.compare_digest`); four roles → capabilities, enforced server-side and reflected in the UI |
| Abuse | Per-identity token buckets, lockout after 8 failed logins per 300 s, uniform error text (no user enumeration), every refusal audited |
| Degraded operation | Storage failure → last good snapshot forked and flagged; 3 consecutive failures → `READ_ONLY` (reads, what-if and comparison continue); AI outage → template phrasing, **numbers unchanged**; network loss → jittered reconnect with sequence replay and an honest "gap repaired" notice |
| Evidence | Append-only audit chain, verified on demand and on health checks; every AI invocation logged with its prompt digest and grounding result |
| Air gap | No CDN, no web fonts, no tile server, no outbound calls: the map is drawn from the twin's own geometry and the model runs on the appliance |

---

## Testing

```bash
backend/.venv/bin/python scripts/smoke.py --base-url http://127.0.0.1:8000   # end-to-end
backend/.venv/bin/python scripts/export_schema.py --check                     # schema drift
cd frontend && npx tsc --noEmit                                               # console contract
```

The smoke test is the acceptance gate: if it passes, the loop works — collect,
fuse, understand, predict, simulate, recommend, commit, compare, audit.

---

## Scope, honestly

* **One city.** The dataset is Kochi; the schema, API and console are single-city by
  design. Multi-city is a deliberate non-goal for this MVP.
* **Synthetic telemetry.** The sensor mesh replays a physically-consistent synthetic
  stream driven by the drill rainfall (an admin action, audited). Real connectors are
  implemented (`HttpFeedConnector`, `ManualReportConnector`) and the ingest path is
  production-shaped — what is missing is a live feed contract, not a code path.
* **Lumped hydrology, not 2-D.** Ward-scale mass balance with a ponding fraction.
  The assumption register says so on every run, in the console, and at
  `/api/v1/simulations/meta/assumptions`.
* **The language model is optional.** CUSTOS runs with `CUSTOS_AI_ENABLED=false` and
  every KPI, cascade and recommendation is unchanged; only the phrasing of narratives
  falls back to templates.
* **SQLite in dev, PostgreSQL in production.** The code path is the same; the
  deployment target is PostgreSQL with an async `psycopg` pool.
