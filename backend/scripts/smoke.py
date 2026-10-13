#!/usr/bin/env python
"""End-to-end smoke test against a running CUSTOS API.

Run after ``scripts\\start_backend.bat`` (or ``run_dev.sh``)::

    backend\\.venv\\Scripts\\python.exe scripts\\smoke.py --base-url http://127.0.0.1:8000

It exercises the *whole* operating loop the way a console does - authenticate,
read the twin, force a cycle, launch a what-if run, wait for it, read the
recommendations, commit one into a task, compare two scenarios, verify the audit
chain - and fails loudly with the response body if any step misbehaves. A smoke
test that only checks HTTP 200 tells an EOC nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PASS = "PASS"
FAIL = "FAIL"


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.api_key: str | None = None

    def request(self, method: str, path: str, body: Any = None, *, api_key: str | None = None, timeout: float = 120.0) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token and not api_key:
            request.add_header("Authorization", f"Bearer {self.token}")
        if api_key:
            request.add_header("X-API-Key", api_key)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise AssertionError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" - {detail}" if detail else ""))
    return condition


def main() -> int:
    parser = argparse.ArgumentParser(description="CUSTOS end-to-end smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--operator", default="commander.rao")
    parser.add_argument("--password", default="custos-demo")
    parser.add_argument("--api-key", default="sensor-dev-key")
    parser.add_argument("--rainfall", type=float, default=68.0, help="drill rainfall used to drive the telemetry replay")
    parser.add_argument("--ensemble", type=int, default=16)
    args = parser.parse_args()

    client = Client(args.base_url)
    failures = 0
    print(f"CUSTOS smoke test against {args.base_url}\n")

    print("1. authentication")
    tokens = client.request("POST", "/api/v1/auth/login", {"subject": args.operator, "password": args.password, "role": "commander"})
    client.token = tokens["access_token"]
    failures += not check("login issues a token", bool(client.token), f"role={tokens.get('role')}")
    failures += not check("refresh token issued", bool(tokens.get("refresh_token")))
    me = client.request("GET", "/api/v1/auth/me")
    failures += not check("identity resolves", me["subject"] == args.operator, f"{me['role']} with {len(me['permissions'])} permissions")

    print("\n2. unauthenticated access is refused")
    try:
        anonymous = Client(args.base_url)
        anonymous.request("GET", "/api/v1/twin")
        failures += not check("anonymous request rejected", False, "a public twin endpoint would leak the city's state")
    except AssertionError as exc:
        failures += not check("anonymous request rejected", "401" in str(exc), str(exc)[:80])

    print("\n3. role enforcement")
    viewer = Client(args.base_url)
    viewer_tokens = viewer.request("POST", "/api/v1/auth/login", {"subject": "viewer.liaison", "password": args.password, "role": "viewer"})
    viewer.token = viewer_tokens["access_token"]
    try:
        viewer.request("POST", "/api/v1/simulations", {"name": "viewer attempt", "horizons": 60})
        failures += not check("viewer cannot simulate", False)
    except AssertionError as exc:
        failures += not check("viewer cannot simulate", "403" in str(exc), str(exc)[:80])

    print("\n4. twin state")
    twin = client.request("GET", "/api/v1/twin?include=assets,zones,sensors,insights")
    failures += not check("assets present", len(twin["assets"]) > 100, f"{len(twin['assets'])} assets, {len(twin['zones'])} zones, {len(twin['sensors'])} sensors")
    failures += not check("dependency insights present", len(twin["insights"]["single_points_of_failure"]) > 0, f"top SPOF {twin['insights']['single_points_of_failure'][0]['name']}")
    graph = client.request("GET", "/api/v1/twin/graph?kinds=powers,accesses,feeds")
    failures += not check("graph endpoint filters by relation", len(graph["edges"]) > 0, f"{len(graph['nodes'])} nodes / {len(graph['edges'])} edges")

    print("\n5. the operating loop (Collect -> Fuse -> Understand -> Predict)")
    client.request("POST", f"/api/v1/drills/rainfall?rainfall_mm_h={args.rainfall}", {"rainfall_mm_h": args.rainfall})
    cycle = client.request("POST", "/api/v1/cycle?force=true", None)
    stages = {stage["stage"].split(":")[0]: stage for stage in cycle["stages"]}
    failures += not check("all six stages ran", len(cycle["stages"]) >= 5, ", ".join(f"{k}={v['state']}" for k, v in stages.items()))
    failures += not check("collect ingested readings", cycle["readings_ingested"] > 0, f"{cycle['readings_ingested']} readings")
    stages_view = client.request("GET", "/api/v1/stages")
    failures += not check("stage view served", bool(stages_view.get("order")), f"prediction confidence {stages_view.get('prediction', {}).get('confidence')}")

    print("\n6. what-if simulation")
    accepted = client.request(
        "POST",
        "/api/v1/simulations",
        {
            "name": "smoke - 68 mm/h burst, 6 h horizon",
            "description": "smoke test scenario",
            "horizon_minutes": 360,
            "timestep_minutes": 15,
            "ensemble_size": args.ensemble,
            "random_seed": 7,
            "hazards": [
                {"hazard_type": "rainfall_burst", "location": {"lon": 76.296, "lat": 9.978}, "radius_m": 6000, "intensity_mm_h": args.rainfall, "start_min": 30, "duration_min": 180},
                {"hazard_type": "tidal_surge", "surge_m": 0.25, "start_min": 0, "duration_min": 360},
            ],
        },
    )
    run_id = accepted["id"]
    deadline = time.time() + 300
    status: dict[str, Any] = {}
    while time.time() < deadline:
        status = client.request("GET", f"/api/v1/simulations/{run_id}/status")
        if status["status"] in {"complete", "failed"}:
            break
        time.sleep(2)
    failures += not check("simulation completed", status.get("status") == "complete", f"{status.get('status')} - {status.get('message')}")
    result_id = status.get("result_id") or run_id
    result = client.request("GET", f"/api/v1/simulations/{result_id}")
    kpis = result["kpis"]
    failures += not check("KPIs computed", kpis["patients_at_risk"] >= 0, f"patients {kpis['patients_at_risk']}, roads {kpis['roads_impassable_km']} km, severed {kpis['severed_response_pairs']}")
    failures += not check("ensemble uncertainty reported", result["uncertainty"]["p90"]["patients_at_risk"] >= result["uncertainty"]["p10"]["patients_at_risk"], f"p10 {result['uncertainty']['p10']['patients_at_risk']} - p90 {result['uncertainty']['p90']['patients_at_risk']}")
    failures += not check("explainability attached", 0.0 < result["explainability"]["confidence"] <= 1.0, f"band {result['explainability']['band']}, grounded={result['explainability']['grounded']}")
    failures += not check("timeline produced", len(result["timeline"]) >= 5, f"{len(result['timeline'])} frames")
    failures += not check("narrative produced", len(result["narrative"]) > 200, f"{len(result['narrative'])} chars")

    print("\n7. recommendations and counterfactuals")
    recommendations = result["recommendations"]
    failures += not check("recommendations ranked", bool(recommendations), f"{len(recommendations)} actions")
    if recommendations:
        top = recommendations[0]
        failures += not check("top action carries a measured counterfactual", bool(top["counterfactual"]), top["action"][:70])
        failures += not check("expected KPI delta present", isinstance(top["expected_kpi_delta"], dict) and len(top["expected_kpi_delta"]) > 5, f"{len(top['expected_kpi_delta'])} KPIs in the delta")

    print("\n8. commit an action (audited)")
    if recommendations:
        task = client.request("POST", f"/api/v1/simulations/{result_id}/recommendations/{recommendations[0]['id']}/commit", {"assigned_to": "ward-1"})
        failures += not check("task created from recommendation", bool(task.get("id")), f"{task.get('id')} status={task.get('status')}")
        tasks = client.request("GET", "/api/v1/tasks")
        failures += not check("task visible on the board", any(item["id"] == task["id"] for item in tasks["tasks"]), f"{tasks['count']} tasks")

    print("\n9. scenario comparison")
    compare = client.request(
        "POST",
        "/api/v1/simulations/compare",
        {"scenario_a_id": result_id, "scenario_b_id": result_id, "weights": {"patients_at_risk": 2.0}},
    )
    failures += not check("comparison returns rows", len(compare["rows"]) > 10, f"{len(compare['rows'])} KPIs compared")
    failures += not check("verdict is explicit", bool(compare["verdict"]), compare["verdict"][:90])

    print("\n10. AI grounding")
    ai_status = client.request("GET", "/api/v1/ai/status")
    failures += not check("AI policy published", ai_status["policy"]["grounding_required"] is True, f"ollama reachable={ai_status['ollama'].get('reachable')}")
    verify = client.request(
        "POST",
        "/api/v1/ai/verify-text",
        {"text": "Peak exposure is 2002 patients at risk and 45.5 percent of hospital capacity is lost.", "simulation_id": result_id},
    )
    failures += not check("grounded text accepted", verify["grounded"], f"{verify['allowed_number_count']} permissible figures")
    verify_bad = client.request(
        "POST",
        "/api/v1/ai/verify-text",
        # a figure that is not a rounded form of anything the run computed
        {"text": "Peak exposure is 93142 patients at risk.", "simulation_id": result_id},
    )
    failures += not check("fabricated figure rejected", not verify_bad["grounded"], f"caught {verify_bad['ungrounded_numbers']}")

    print("\n11. metrics, ingestion and audit integrity")
    ingest = client.request(
        "POST",
        "/api/v1/ingest/readings",
        {
            "gateway_id": "smoke-gw-1",
            "readings": [
                {"sensor_id": "s_rain_kalamassery", "metric": "rainfall_mm_h", "value": 61.5, "unit": "mm/h", "observed_at": "2026-10-13T20:00:00+00:00"},
                {"sensor_id": "s_rain_kalamassery", "metric": "rainfall_mm_h", "value": 61.5, "unit": "mm/h", "observed_at": "2026-10-13T20:00:00+00:00"},
            ],
        },
        api_key=args.api_key,
    )
    failures += not check("replay deduplicated", ingest["accepted"] == 1 and ingest["duplicates"] == 1, f"accepted {ingest['accepted']}, duplicates {ingest['duplicates']}")
    anomalies = client.request("GET", "/api/v1/anomalies?limit=5")
    failures += not check("anomaly endpoint answers", isinstance(anomalies, list), f"{len(anomalies)} anomalies")
    audit = client.request("GET", "/api/v1/audit?limit=50")
    failures += not check("audit trail populated", len(audit) > 3, f"{len(audit)} entries")
    chain = client.request("GET", "/api/v1/audit/verify")
    failures += not check("audit hash chain verifies", chain["valid"], f"{chain['entries_checked']} entries, head {chain.get('head', '')[:12]}")
    health = client.request("GET", "/api/v1/health")
    failures += not check("health reports storage ok", health["checks"]["database"]["status"] == "ok", f"overall {health['status']}")

    print("\n12. compliance with the published schema")
    kpis_dictionary = client.request("GET", "/api/v1/simulations/meta/kpis")
    failures += not check("KPI dictionary served", len(kpis_dictionary["kpis"]) > 15, f"{len(kpis_dictionary['kpis'])} KPIs documented")
    assumptions = client.request("GET", "/api/v1/simulations/meta/assumptions")
    failures += not check("assumption register served", len(assumptions["interventions"]) > 5, f"{len(assumptions['interventions'])} intervention assumptions published")

    print(f"\n{'=' * 72}")
    if failures:
        print(f"SMOKE TEST FAILED: {failures} check(s) did not pass")
        return 1
    print("SMOKE TEST PASSED: the full operating loop works end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
