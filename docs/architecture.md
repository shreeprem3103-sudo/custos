# CUSTOS — System Architecture

> **One city. One appliance. Six stages: Collect → Fuse → Understand → Predict → Simulate → Recommend.**

CUSTOS is a *Crisis Digital Twin*. It exists to move an Emergency Operations Centre
from reacting to a flood to pre-empting it: the same water is modelled hours before
it arrives, the dependencies that turn a flooded road into a blacked-out hospital
are computed explicitly, and every recommendation carries the counterfactual run
that justified it.

This document is the map of the system. Everything here is implemented; the
repository is the authority when the two disagree.

---

## 1. End-to-end architecture

```mermaid
flowchart TB
    subgraph FIELD["Field & external reality"]
        SENS["Sensor mesh<br />40 rain / stage / depth gauges"]
        SCADA["Utility SCADA<br />substations, pump stations"]
        MET["Weather &amp; tide feed<br />intensity + spring-tide prediction"]
        PHONE["Telephone / radio reports<br />operator observations"]
    end

    subgraph COLLECT["1 · COLLECT — connectors"]
        REGISTRY["Connector registry<br />KochiReplay · ManualObservation · HttpFeed"]
        NORM["Normalisation<br />timestamps → UTC · units → canonical · coordinates → WGS 84<br />identity resolution · dedupe_key"]
        GATE["Store-and-forward gateway ingest<br />replay is a no-op, never a double count"]
    end

    subgraph FUSE["2 · FUSE — fusion layer"]
        ROBUST["Robust baselines<br />median + 1.4826·MAD · z clamped ±8"]
        TWINCACHE["Twin snapshot cache (1.5 s TTL)<br />flooded last-good snapshot when storage fails"]
        DIGEST["Digest + provenance<br />every snapshot is hash-addressable"]
    end

    subgraph UNDERSTAND["3 · UNDERSTAND — graph &amp; anomaly"]
        GRAPH["Dependency graph<br />464 relationship edges, transmission = criticality × attenuation × redundancy"]
        INSIGHTS["Graph insights<br />single points of failure · betweenness · reach · cascade depth"]
        ANOM["Anomaly detection<br />robust z ≥ 3.5 against a 90-minute baseline"]
    end

    subgraph PREDICT["4 · PREDICT — hydrology"]
        HYDRO["Lumped mass balance per water body<br />canal + backwater + tide, ponding over 28 % of ward area"]
        FORECAST["30-minute forecast with confidence<br />from observed trend and sample count"]
    end

    subgraph SIMULATE["5 · SIMULATE — what-if ensemble"]
        ENSEMBLE["Monte-Carlo ensemble (24–48 members)<br />asyncio.to_thread behind a concurrency semaphore"]
        FACIL["Facility loss curves<br />hospital / shelter / pump / substation depth bands"]
        MOBILITY["Mobility routing<br />edges where depth exceeds threshold are severed, not slowed"]
        KPIS["16 KPIs with p10 / p50 / p90 bands"]
    end

    subgraph RECOMMEND["6 · RECOMMEND — decision support"]
        CAND["Candidate generator (9 levers)"]
        COUNTER["Counterfactual re-simulation<br />equal-size, same-seed control on both legs"]
        RANK["Ranking<br />lever prior × feasibility × urgency, ≤ 2 per lever"]
    end

    subgraph ASSIST["Local AI (on-premise)"]
        OLLAMA["Ollama · qwen2.5:7b-instruct<br />127.0.0.1:11434, temperature 0.1, JSON mode"]
        GROUND["Grounding verifier<br />every numeral must be a rounded form of a computed value"]
        CIRCUIT["Circuit breaker<br />3 failures → 60 s open → template phrasing"]
    end

    subgraph STORE["Storage — PostgreSQL (async psycopg) / SQLite in dev"]
        PG[("26 tables<br />twin · simulations · incidents · tasks<br />audit_log (sha256 chain) · events")]
        CKPT[("LangGraph async checkpoints<br />reasoning state that survives a restart")]
    end

    subgraph SERVE["Command &amp; control API (FastAPI, async)"]
        API["REST /api/v1<br />JWT HS256 + API keys, RBAC, token buckets, rate limits"]
        WS["WebSocket /api/v1/stream<br />bounded queues · seq replay · 15 s heartbeats"]
        CONSOLE["Command console (static bundle, served at /console)<br />same origin, no CORS, no CDN"]
    end

    SENS --> REGISTRY
    SCADA --> REGISTRY
    MET --> REGISTRY
    PHONE --> GATE
    REGISTRY --> NORM
    GATE --> NORM
    NORM --> ROBUST --> TWINCACHE --> DIGEST
    DIGEST --> GRAPH --> INSIGHTS
    DIGEST --> ANOM
    INSIGHTS --> HYDRO
    DIGEST --> HYDRO
    HYDRO --> FORECAST
    FORECAST -->|"≥ 12 mm/h or operator request"| ENSEMBLE
    INSIGHTS --> ENSEMBLE
    ENSEMBLE --> FACIL --> MOBILITY --> KPIS
    KPIS --> CAND --> COUNTER --> RANK
    RANK --> GROUND
    OLLAMA --> GROUND
    GROUND -.->|"ungrounded draft rejected"| CIRCUIT
    CIRCUIT -.->|"deterministic phrasing"| API
    RANK --> PG
    KPIS --> PG
    TWINCACHE --> PG
    GRAPH --> PG
    GROUND --> CKPT
    APIMEM(("Pipeline orchestrator<br />six stages, one cycle every 45 s – 3 min")) --> COLLECT
    APIMEM --> FUSE
    APIMEM --> UNDERSTAND
    APIMEM --> PREDICT
    APIMEM --> SIMULATE
    APIMEM --> RECOMMEND
    PG --> API
    PG --> WS
    API --> CONSOLE
    WS --> CONSOLE
    CONSOLE -->|"operator commits an action"| API
    API --> PG
```

### Why the arrows point that way

| Decision | Reason |
| --- | --- |
| The **pipeline orchestrator is the only writer** of stage state, twin snapshots and cycle reports | One writer means the console can never disagree with the backend about what ran, and every stage carries its own `status`, `detail`, timestamps and degradation flag. |
| **Storage failure forks the last good snapshot** rather than raising | A database hiccup during a flood must not blind the console. The snapshot is marked `degraded=True`, the API emits `X-CUSTOS-Degraded`, and the UI says so in the banner. |
| The **LLM is a leaf, never a source** | The model can only rephrase text that engines produced. The grounding verifier re-checks every numeral (2 % relative / 1 absolute, precision-aware) and a rejected draft is logged, audited and replaced by a template. A model outage changes the wording, never a number. |
| The console is a **static bundle served by the API at `/console`** | Air-gapped operation: one process, one port, no CORS, no CDN, no tile server. The map is drawn from the twin's own geometry. |

---

## 2. The six-stage cycle

```mermaid
sequenceDiagram
    autonumber
    participant Sched as Scheduler (45 s heavy / 3 min calm)
    participant Pipe as Pipeline orchestrator
    participant Conn as Connector registry
    participant DB as PostgreSQL
    participant Graph as Dependency graph
    participant Hydro as Hydrology engine
    participant Sim as Simulation ensemble
    participant Rec as Recommender + grounding verifier
    participant Bus as Event bus
    participant UI as Command console

    Sched->>Pipe: run_cycle(trigger, force?)
    Pipe->>Conn: poll_all()
    Conn->>DB: insert readings (dedupe_key UNIQUE → replay-safe)
    Pipe->>Bus: publish sensor.reading
    Pipe->>DB: read fused window, build snapshot
    DB-->>Pipe: 117 assets · 464 edges · 40 sensors
    Pipe->>Graph: insights() → SPOFs, betweenness, reach
    Pipe->>Hydro: forecast 30 min ahead (confidence, basis)
    alt forecast ≥ 12 mm/h or force
        Pipe->>Sim: run ensemble (24–48 members, thread pool)
        Sim-->>Pipe: 16 KPIs + p10/p50/p90 + timeline + cascade
        Pipe->>Rec: recommend() → candidates
        Rec->>Sim: counterfactual per candidate (same seed, same size)
        Sim-->>Rec: measured Δ per KPI
        Rec->>Rec: grounding verify phrasing
        Rec->>Bus: publish simulation.complete · recommendation.new
    else quiet city
        Pipe->>Pipe: predict only (persistence), 30-minute horizon
    end
    Pipe->>DB: persist snapshot, cycle report, audit entry
    Pipe->>Bus: publish twin.snapshot (digest) · heartbeat every 15 s
    Bus-->>UI: /api/v1/stream (bounded queue, drop-oldest, seq replay)
    UI->>Pipe: operator forces a cycle or runs a what-if
```

---

## 3. Data model (Crisis Digital Twin)

```mermaid
erDiagram
    ASSETS ||--o{ RELATIONSHIPS : "source of"
    ASSETS ||--o{ RELATIONSHIPS : "target of"
    ASSETS ||--o| ROAD_DETAILS : "extends"
    ASSETS ||--o| FACILITY_DETAILS : "extends"
    ASSETS ||--o| SHELTER_DETAILS : "extends"
    ZONES ||--o{ ASSETS : "contains"
    ASSETS ||--o{ SENSORS : "instrumented by"
    SENSORS ||--o{ SENSOR_READINGS : "reports"
    SENSORS ||--o{ ANOMALIES : "raises"
    INCIDENTS ||--o{ INCIDENT_ASSETS : "affects"
    INCIDENTS ||--o{ INCIDENT_EVENTS : "timeline"
    INCIDENTS ||--o{ INCIDENT_UPDATES : "updates"
    INCIDENTS ||--o{ TASKS : "spawns"
    SIMULATIONS ||--o{ CASCADE_EDGES : "derives"
    SIMULATIONS ||--o{ RECOMMENDATIONS : "produces"
    SIMULATIONS ||--o{ SIMULATION_EVENTS : "progress"
    SIMULATIONS ||--o{ TASKS : "justifies"
    RECOMMENDATIONS ||--o{ TASKS : "committed as"
    RESOURCES ||--o{ RESOURCE_ASSIGNMENTS : "allocated by"
    TASKS ||--o{ RESOURCE_ASSIGNMENTS : "consumes"
    AUDIT_LOG ||--|| AUDIT_LOG : "prev_hash chain"
    CONNECTOR_RUNS }o--|| ASSETS : "ingests for"
    AI_INVOCATIONS ||--o{ CHECKPOINT_MIRROR : "reasoning state"

    ASSETS {
        string id PK
        string name
        string asset_type "hospital, substation, road, pump_station…"
        float criticality "0..1 — how much the city depends on it"
        float elevation_m
        json geometry "GeoJSON point or linestring"
        float lon
        float lat
        string status "ok / degraded / impaired / failed"
        float flood_depth_m
        bool power_available
        bool access_available
        float service_level "0..1 of design service"
        json attributes "extras, never silently dropped"
    }
    RELATIONSHIPS {
        string id PK
        string source_id FK
        string target_id FK
        string kind "feeds, powers, accesses, drains, monitors…"
        float criticality
        float attenuation "fraction of parent impact transmitted"
        int lead_time_min
        string failure_mode "immediate, cascade, latent"
        float recovery_hours
        string redundancy_group
    }
    ZONES {
        string id PK
        string name
        float lon
        float lat
        int population
        float vulnerability_index
        float population_density "people per km²"
        float mean_elevation_m
        float flood_depth_m
    }
    SENSOR_READINGS {
        string id PK
        string sensor_id FK
        string metric "canonical vocabulary"
        float value
        string unit
        datetime observed_at "always UTC"
        float quality "0..1, travels to the UI"
        string dedupe_key UK "a replay is a no-op"
    }
    SIMULATIONS {
        string id PK
        json request "the whole scenario"
        json kpis "16 KPIs"
        json uncertainty "p10 / p50 / p90"
        json timeline "frame by frame"
        json cascade "explainable impact edges"
        json recommendations "with measured counterfactuals"
        json explainability "confidence, freshness, assumptions"
        string determinism_token "same input → same numbers"
        bool degraded
        int runtime_ms
    }
    AUDIT_LOG {
        int seq PK
        string actor
        string role
        string action
        string resource
        string outcome
        json detail
        string hash "sha256 of prev_hash+seq+at+actor+action+resource+outcome+detail"
        string prev_hash
    }
```

`backend/schema/custos_schema.sql` (PostgreSQL DDL) and `backend/schema/schema.prisma`
are generated from the same models and verified in CI by
`python backend/scripts/export_schema.py --check`.

---

## 4. Dependency graph — the reason CUSTOS is not a dashboard

```mermaid
flowchart LR
    KALOOR["Kaloor 220 kV substation<br />criticality 0.95 · 38 links"]
    PUMP["Thevara-Perandoor pump station"]
    CANAL["Thevara-Perandoor canal"]
    HOSP["Hospital cluster<br />(Kalamassery, General, Amrita)"]
    TOWER["Telecom towers"]
    ROADS["Ambulance corridors<br />Kaloor–Kadavanthra, Thevara Ferry"]
    SHELTER["Shelters<br />(JLNS, Maharaja's, St Teresa's)"]

    KALOOR -->|"powers · attenuation 1.0"| PUMP
    KALOOR -->|"powers"| HOSP
    KALOOR -->|"powers"| TOWER
    CANAL -->|"drains · lead time 45 min"| PUMP
    PUMP -->|"drains"| ROADS
    ROADS -->|"accesses · severed ≥ 0.35 m"| HOSP
    ROADS -->|"accesses"| SHELTER
    HOSP -.->|"generator autonomy 4 h, then service 0"| HOSP
```

Transmission along an edge is
`criticality × attenuation × (0.55 + 0.45·criticality_of_source) × max(0.28, 1/(1 + 0.55·healthy_alternatives))`
and maps to bands: **≥ 0.85 FAILED, ≥ 0.60 IMPAIRED, ≥ 0.35 DEGRADED**.
`MONITORS` edges carry knowledge only — a sensor losing power does not disable the
pump it watches, and the model says so rather than inventing a cascade.

Two-hop reachability is what makes the Kaloor substation the city's real
vulnerability: losing it impairs 55 assets within 3.5 ms of graph traversal,
including three hospitals, before the flood has reached them.

---

## 5. Resilience: what happens when things break

```mermaid
stateDiagram-v2
    [*] --> Normal
    Normal --> Degraded: storage fails 1× / AI unreachable / connector stale
    Degraded --> ReadOnly: 3 consecutive storage failures
    ReadOnly --> Degraded: a successful write
    Degraded --> Normal: all checks healthy again
    Normal --> DegradedStorage: DB write fails mid-cycle
    DegradedStorage --> Normal: retry succeeds
    DegradedStorage --> ReadOnly: 3 consecutive failures
    note right of Degraded
        Console banner: DEGRADED OPERATION.
        Every panel shows the age of what it renders.
        LLM phrases via templates; numbers unchanged.
    end note
    note right of ReadOnly
        Writes rejected with 503 + audit entry.
        Reads, what-if simulation and comparison continue
        from the last good snapshot (forked, degraded=True).
    end note
```

| Failure | Behaviour | What the operator sees |
| --- | --- | --- |
| Database unreachable | Snapshot forks from last good state, `degraded=True` | Amber banner, `X-CUSTOS-Degraded: true`, per-panel ages |
| 3 consecutive DB failures | Health mode → `READ_ONLY`; writes refused | Red banner; reads, what-if and comparison still work |
| Ollama unreachable | Circuit opens 60 s, template phrasing | "local model offline" chip; **numbers unchanged** |
| Sensor mesh silent | Reading ages rise; quality score decays | Sensor age column; stale-sensor count on the rail |
| Network to the browser | WebSocket reconnects with jittered backoff, resumes from `seq` | "feed down" chip, reconnect count, gap repair notice |
| Event ring exhausted | Heartbeat reports the window; console refetches the twin | "gap repaired" chip — never a silent hole |
| Language model emits a fabricated figure | Grounding verifier rejects the draft, logs it, falls back | "template fallback used" in run evidence |

---

## 6. Deployment topology (single appliance, air-gap friendly)

```mermaid
flowchart LR
    subgraph EOC["Emergency Operations Centre appliance (Windows or Linux)"]
        direction TB
        BAT["scripts\\start_custos.bat<br />supervisor: health-gates each step"]
        VENV["backend\\.venv (isolated)"]
        UVICORN["uvicorn · 0.0.0.0:8000<br />API + WebSocket + /console"]
        PG[("PostgreSQL 14+<br />async psycopg pool")]
        OLLAMA["Ollama · 127.0.0.1:11434<br />qwen2.5:7b-instruct"]
    end
    subgraph OPS["Operators"]
        WS1["Commander workstation<br />/console (same origin)"]
        WS2["Analyst workstation"]
        WALL["Wall display"]
    end
    subgraph EDGE["Field edge"]
        GW["Field gateways<br />store-and-forward, API key auth"]
    end
    BAT --> VENV --> UVICORN
    UVICORN --> PG
    UVICORN --> OLLAMA
    WALL --> UVICORN
    WS1 --> UVICORN
    WS2 --> UVICORN
    GW -->|"POST /api/v1/ingest/readings"| UVICORN
```

No component requires the public internet: the map is drawn from the twin's own
geometry, the fonts are system fonts, and the language model runs on the same box.

---

## 7. Security posture

| Control | Implementation |
| --- | --- |
| Authentication | JWT HS256 (`aud=custos-ui`) for operators; per-user salted sha256 roster entries (`subject:hash$salt:role`); API keys (`key:role`) for ingest, compared with `hmac.compare_digest` |
| Authorisation | Four roles (`viewer`, `analyst`, `commander`, `admin`) mapped to capabilities; the console hides what a role cannot do and the API refuses it anyway |
| Brute force | 8 failures per subject per 300 s triggers lockout; constant-time decoy comparison so user enumeration is not possible |
| Rate limiting | Per-identity token bucket; 429 responses carry the correlation id |
| Transport | Same-origin console; `CORS` allow-list is explicit in production and only loosened in development |
| Secrets | Never in git: `.env.dev` is generated locally from `.env.dev.example`, with a fresh JWT secret and freshly salted rosters |
| Audit | Append-only sha256 chain (`prev_hash` → `hash`), sequence numbers assigned in-process, verified on demand and on every health check; a tampered row is detectable at an exact `seq` |
| Grounding | The LLM cannot introduce a number; rejected drafts are persisted with the prompt digest in `ai_invocations` |
| Input handling | Pydantic models at every boundary; unit, timestamp and coordinate normalisation before storage; unknown fields rejected rather than ignored |
