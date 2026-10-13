# CUSTOS — Operations Runbook

Written for the person on shift at 02:00 during a flood. Short sentences, exact
commands, and the *first* thing to check for each symptom.

---

## 1. Start / stop

| Task | Windows | Linux |
| --- | --- | --- |
| First-time setup | `scripts\setup_windows.bat` | `cd backend && ./run_dev.sh` (creates `.venv` and `.env.dev` itself) |
| Start everything | `scripts\start_custos.bat` | `cd backend && ./run_dev.sh` then build the console with `cd frontend && npm run build` |
| API only | `scripts\start_backend.bat 8000` | `cd backend && ./run_dev.sh --port 8000` |
| Console with hot reload (UI work) | `scripts\start_frontend.bat dev` | `cd frontend && npm run dev` |
| Stop everything | `scripts\stop_custos.bat` | `Ctrl-C` each process |
| Verify | `scripts\smoke_windows.bat` | `backend/.venv/bin/python backend/scripts/smoke.py` |

Endpoints once running:

* Console — `http://127.0.0.1:8000/console/`
* API docs — `http://127.0.0.1:8000/api/docs`
* Health — `http://127.0.0.1:8000/api/v1/health`
* Readiness (used by the start script's gate) — `http://127.0.0.1:8000/api/v1/ready`

---

## 2. The console in one screen

```
┌─────────────┬──────────────────────────────────────┬──────────────────┐
│ Operating   │                                      │ What-If          │
│ loop        │        Operational map (WebGL)       │ Compare          │
│ Forecast    │        ward depth · assets ·         │ Incidents        │
│ City state  │        roads · cascade arcs          │ Evidence         │
│ Worst       │                                      │                  │
│ assets      │        timeline scrubber (scenario)  │                  │
│ SPOFs       │                                      │                  │
├─────────────┴──────────────────────────────────────┴──────────────────┤
│ live event ticker               (feed state · sequence · topic filter)│
└───────────────────────────────────────────────────────────────────────┘
```

Read the top bar first: city · **twin age** · **live feed** · mode · digest. If
"twin" is amber or red, every number below it is that old.

---

## 3. Symptom → cause → action

### "The console will not load; the API responds"

*If `/console` shows "the command console is not built into this deployment"* —
the static bundle is missing. Run `scripts\start_frontend.bat` (or
`cd frontend && npm install && npm run build`) and reload. The API and every
endpoint are unaffected.

### "Feed down" chip is red, panels still update

The WebSocket is down but REST polling continues (20 s twin, 60 s operations). Check
the ticker's reconnect count. Sustained failure with working REST usually means a
proxy that does not upgrade WebSocket connections — terminate `wss://` at the proxy
or use the console on the same origin as the API.

### "mode degraded" or "READ_ONLY"

`GET /api/v1/health` lists each check. `checks.database.status`, `checks.ai`,
`checks.audit_chain`, `checks.connectors`.

* `database: degraded` — storage is failing; the twin is being served from the last
  good snapshot. Check the PostgreSQL service and the connection string in
  `backend/.env.dev`.
* Three consecutive storage failures move the appliance to `READ_ONLY`: reads,
  what-if simulation and comparison keep working, writes are refused with HTTP 503
  **and an audit entry** — the refusal is part of the record, not a silent drop.
* `ai: unreachable` — expected if Ollama is not installed. Numbers are unaffected;
  narrative phrasing falls back to templates. Nothing to do mid-incident.

### "The pipeline stopped at stage 3 (understand)"

`GET /api/v1/stages` returns each stage's `state`, `detail` and `last_run_at`.
Force a pass and watch the log:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/cycle?force=true -H "Authorization: Bearer <token>"
```

The response names the stage that degraded and why (for example "11 sensors could
not be bound to an asset — reported, not silenced"). Unbound sensors are a *data*
problem: fix the sensor→asset mapping in the dataset, not the engine.

### "The what-if run is taking a long time"

Ensemble runtime scales linearly with member count: ~20 s for 10 members against the
full city on a laptop, ~1.5 s for the engines themselves on the graph and hydrology
stages. The run continues server-side even if the browser gives up; it will appear in
the run history. For a commander who needs an answer *now*, use the `burst` preset at
12 members, then re-run at 32 for the decision record.

### "A number looks wrong"

1. Open **Evidence → Run evidence**: the p10/p50/p90 table shows the spread. A value
   near p10 or p90 is the ensemble speaking, not an error.
2. Check the **assumption register** for the run; lumped hydrology and ponding
   concentration are stated explicitly.
3. Sanity check the invariant: a **control run** (What-If → "Control run", no forcing)
   must produce all-zero KPIs and an equity index of 1.00. If it does not, stop using
   the scenario output and file it — that is a defect, not a forecast.

### "The audit chain is broken"

`GET /api/v1/audit/verify` returns `{valid:false, broken_at_seq:N}`. That is the
tamper-evidence working as designed: a row at or before `N` was edited outside the
application. Preserve the database file, export `GET /api/v1/audit?limit=1000`, and
treat every decision after `N` as unverified until the gap is explained. Do **not**
delete or "repair" rows.

### "An operator cannot sign in"

Failed logins lock a subject for 300 s after 8 attempts. Check
`GET /api/v1/audit?action=login` for the pattern. The API deliberately returns the
same message for an unknown user and a wrong password — that is not a bug to "fix".

---

## 4. Drills and training

Drive the synthetic telemetry so the room can practise without waiting for weather:

```bash
# as an admin/commander, from the console: "Drill" button, or:
curl -s -X POST http://127.0.0.1:8000/api/v1/drills/rainfall \
  -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
  -d '{"rainfall_mm_h": 68}'
```

Reference points for a 68 mm/h burst over the central wards (24-member ensemble):

| Signal | Value |
| --- | --- |
| Patients at risk | ~2 000 |
| Hospital capacity lost | ~45 % |
| People requiring rescue | ~21 000 |
| Shelter shortfall | ~129 000 places |
| Road severances | ~22 pairs, ~28 km impassable |
| Population without power | ~716 000 |

Set the drill back to `0` and the next cycle returns the city to a dry baseline.

---

## 5. Backup and retention

* **Back up** the database (or `backend/custos_dev.db` on a laptop) plus
  `backend/.env.dev`. Together they are the entire operational state.
* **Retention**: raw readings age out via `purge_old_readings` (30 days by default);
  aggregated twin history, simulations, incidents, tasks and the audit chain do not.
  A 250 GB appliance volume holds roughly nine months of the 40-sensor raw stream.
* **Never delete audit rows.** Closing a database file is fine; rewriting history is
  not, and the chain will say so.

---

## 6. Where the pieces live

| Symptom | File |
| --- | --- |
| Pipeline stages, cycle orchestration | `backend/custos/services/pipeline.py` |
| Dependency cascade, SPOFs | `backend/custos/engines/graph.py` |
| Hydrology, ponding, water bodies | `backend/custos/engines/hydro.py` |
| Ensemble, facilities, mobility | `backend/custos/engines/simulation.py` |
| Ranking + counterfactual re-runs | `backend/custos/engines/recommend.py` |
| KPI definitions (published to the UI) | `backend/custos/domain/kpi_meta.py` |
| Auth, RBAC, rate limits, degraded mode | `backend/custos/api/deps.py` |
| Grounding verifier, local model client | `backend/custos/services/ai.py` |
| Event bus, replay, heartbeats | `backend/custos/realtime/bus.py` |
| Schema (generated) | `backend/schema/custos_schema.sql`, `schema.prisma` |
| Console state and panels | `frontend/src/lib/store.ts`, `frontend/src/components/` |
