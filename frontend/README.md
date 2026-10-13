# CUSTOS command console

The browser console for the CUSTOS Crisis Digital Twin: the operational map, the
What-If engine, scenario comparison, the incident board and the evidence panel.

Next.js 15 (App Router) · TypeScript strict · deck.gl (WebGL) · Zustand.

## Run it

The API serves the built console at `/console`, which is the deployment shape —
one process, one port, no CORS, works air-gapped.

```bash
npm install
npm run build          # writes ./out — the API serves it at http://127.0.0.1:8000/console/
```

For UI work, with the API already running on 8000:

```bash
npm run dev            # http://localhost:3000/console/  (proxies /api to the backend)
```

On Windows, `scripts\start_frontend.bat` does both (`serve` or `dev`).

## Notes for whoever picks this up next

* **`basePath: "/console"`** — the bundle is served from a path prefix. If you move
  the mount point, change it in `next.config.mjs` and in the API's
  `_frontend_dir()` logic together; nothing else assumes a location.
* **Every API call is relative** (`/api/v1/...`) — the console never hard-codes a
  host, so the same build runs in dev, in production and behind a path prefix.
* **No basemap tiles and no web fonts.** An EOC may be air-gapped; the map is drawn
  from the twin's own geometry and the type is the system stack. Please keep it
  that way: a console that needs a CDN is a console that goes blank when the link
  does.
* **State lives in `src/lib/store.ts`.** One typed store with an explicit update
  path per data source (bootstrap, poll, live event, operator action). Do not add
  a second polling loop inside a panel — that is how two panels start disagreeing
  about the same city.
* **Types in `src/lib/types.ts` mirror the backend's Pydantic models** and are
  maintained by hand on purpose: `npm run typecheck` is the drift alarm. A console
  that renders `undefined` during a flood is worse than one that refuses to build.
* **`src/lib/live.ts`** owns reconnect, replay and gap detection. If the API's
  WebSocket message shapes change, change them there — nowhere else speaks the
  protocol.
* **Uncertainty, freshness and confidence travel with the numbers.** KPI cards
  render the ensemble band and the delta against the control run; the top bar
  renders twin age and feed state; the evidence panel renders the assumption list.
  Removing any of those makes the console look cleaner and makes it unwise to
  trust.
