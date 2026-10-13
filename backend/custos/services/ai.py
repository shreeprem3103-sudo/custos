"""Grounded, on-premise AI for the command centre.

Design rules, in priority order:

1. **The engines decide, the model explains.** Every number the operator sees is
   produced by the deterministic engines. The language model may only *rephrase*
   facts it was handed.
2. **Grounding is enforced, not requested.** :func:`numeric_tokens` extracts every
   numeral from the model's draft and :func:`check_grounding` rejects any figure
   that is not present in the structured input (with rounding tolerance). A
   rejected draft is never shown; the template text is returned instead and the
   rejection is logged and audited.
3. **Offline is a first-class state.** The deployment target is an air-gapped EOC
   with Ollama on the same box. If the model is slow, missing or wrong, every
   capability still works: the deterministic fallbacks are the product, and the
   model is an accelerator. ``ai_invocations`` records which path was used so an
   after-action review can tell them apart.

Nothing here imports a hosted API client: this platform must run with the
network cable pulled out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..config import settings
from ..observability import get_logger, redact

log = get_logger("services.ai")

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)*\s*(?:%|mm/h|m³/s|m3/s|m|km|min|h|people|patient[s]?)?", re.IGNORECASE)
_CANONICAL_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# grounding
# --------------------------------------------------------------------------- #
def numeric_tokens(text: str) -> list[str]:
    """Every numeral in ``text``, normalised ("1,234.5" and "1234.50" → 1234.5)."""
    found: list[str] = []
    for match in _CANONICAL_NUMBER_RE.finditer(text.replace(",", "")):
        found.append(match.group(0))
    return found


def _as_float(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


_SCALE_WORDS: dict[str, int] = {
    "thousand": 3,
    "mn": 6,
    "million": 6,
    "billion": 9,
    "lakh": 5,
    "lakhs": 5,
    "crore": 7,
    "crores": 7,
}


def _written_precision(token: str) -> tuple[int, int]:
    """Return ``(decimal_places, trailing_zeros)`` for a numeral as written.

    This is what makes the verifier honest about precision. "2,000" claims only
    that the true value is about two thousand, so 2002.4 satisfies it; "91,234"
    claims five significant figures, so 90103 does *not* - and the earlier
    relative-tolerance rule would have let exactly that fabrication through.
    """
    body = token.replace(",", "").lstrip("+-")
    if "." in body:
        whole, _, fraction = body.partition(".")
        return len(fraction), 0
    stripped = body.lstrip("0")
    if not stripped:
        return 0, 0
    return 0, len(stripped) - len(stripped.rstrip("0"))


def _is_rounded_form_of(candidate: float, token: str, *, scale_zeros: int = 0, abs_tolerance: float = 1.0) -> bool:
    """True when ``candidate``, rounded to the precision ``token`` was written
    with, *is* ``token`` - or matches it to within one unit."""
    value = _as_float(token)
    if value is None:
        return True
    if scale_zeros:
        value *= 10**scale_zeros
    if abs(value - candidate) <= abs_tolerance:
        return True
    decimals, trailing_zeros = _written_precision(token)
    trailing_zeros += scale_zeros
    if trailing_zeros:
        return round(candidate, -trailing_zeros) == value
    if decimals:
        return round(candidate, decimals) == value
    return False


def check_grounding(text: str, allowed: Iterable[float | int | str], *, abs_tolerance: float = 1.0) -> list[str]:
    """Return the numerals in ``text`` that cannot be traced to ``allowed``.

    A numeral is grounded when it is the structured value *rounded to the
    precision the sentence used* (or within one unit of it). Anything else is a
    claim the twin never made: a model may round 716 432 to "716,000", it may
    not write "720,000", and it may never invent a figure at all.
    """
    allowed_values: list[float] = []
    for item in allowed:
        value = _as_float(str(item))
        if value is not None:
            allowed_values.append(value)
    if not allowed_values:
        # nothing to ground against: every numeral is unsupported by construction
        return numeric_tokens(text)
    lowered = text.replace(",", "").lower()
    ungrounded: list[str] = []
    for match in _CANONICAL_NUMBER_RE.finditer(lowered):
        token = match.group(0)
        value = _as_float(token)
        if value is None:
            continue
        tail = lowered[match.end() : match.end() + 12].strip()
        scale_zeros = next(
            (zeros for word, zeros in _SCALE_WORDS.items() if tail.startswith(word)),
            0,
        )
        if any(_is_rounded_form_of(candidate, token, scale_zeros=scale_zeros, abs_tolerance=abs_tolerance) for candidate in allowed_values):
            continue
        ungrounded.append(token)
    return ungrounded


def collect_numbers(*sources: Any) -> set[float]:
    """Flatten every number reachable in structured data (dicts, models, lists)."""
    out: set[float] = set()

    def walk(node: Any) -> None:
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            out.add(float(node))
            return
        if isinstance(node, str):
            for token in numeric_tokens(node):
                parsed = _as_float(token)
                if parsed is not None:
                    out.add(parsed)
            return
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value)
            return
        if isinstance(node, (list, tuple, set)):
            for value in node:
                walk(value)
            return
        for attr in ("model_dump",):
            if hasattr(node, attr):
                walk(getattr(node, attr)())
                return
        if hasattr(node, "__dict__"):
            walk(vars(node))

    walk(sources)
    return out


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AIResult:
    """Outcome of one grounded reasoning call — including when it was refused."""

    text: str
    accepted: bool
    model: str
    fallback_used: bool
    latency_ms: float
    ungrounded_numbers: list[str] = field(default_factory=list)
    rejected_draft: str = ""
    error: str = ""
    prompt_hash: str = ""
    tokens_out: int = 0

    @property
    def provenance(self) -> str:
        if self.model == "deterministic":
            return "deterministic engine text (no language model involved)"
        if self.fallback_used:
            return f"language model {self.model} output rejected on grounding checks; deterministic text shown instead"
        return f"language model {self.model}, grounding verified"


class OllamaClient:
    """Minimal async client for an on-premise Ollama server.

    Deliberately small: one endpoint, JSON-constrained output, a timeout, a
    circuit breaker and a concurrency limit. No retries inside a request — under
    flood load, a retry storm against the GPU box is how the AI takes the EOC
    down with it.
    """

    def __init__(self, *, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self._failures = 0
        self._open_until = 0.0
        self._semaphore = asyncio.Semaphore(max(1, settings.ollama_max_concurrency))

    @property
    def available(self) -> bool:
        return settings.ai_enabled and time.monotonic() >= self._open_until

    async def generate_json(self, prompt: str, *, system: str = "", timeout: float | None = None) -> dict[str, Any]:
        import httpx

        if not self.available:
            raise RuntimeError("ollama circuit breaker open or AI disabled")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "top_p": 0.9, "num_predict": 400},
            "keep_alive": "5m",
        }
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=timeout or settings.ollama_timeout_s) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
                response.raise_for_status()
                body = response.json()
        self._failures = 0
        raw = body.get("response") or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:  # a non-JSON answer is a failed answer
            self._record_failure()
            raise RuntimeError(f"model returned non-JSON output: {exc}") from exc

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= 3:
            self._open_until = time.monotonic() + 60.0
            log.warning("ollama circuit breaker opened for 60 s", extra={"custos": {"failures": self._failures}})

    async def health(self) -> dict[str, Any]:
        import httpx

        if not settings.ai_enabled:
            return {"enabled": False, "reachable": False, "model": self.model}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                tags = [m.get("name") for m in response.json().get("models", [])]
            return {
                "enabled": True,
                "reachable": True,
                "model": self.model,
                "model_present": self.model in tags,
                "models": tags[:10],
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "circuit_open": not self.available,
            }
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "reachable": False, "model": self.model, "error": redact(str(exc)), "circuit_open": not self.available}


# --------------------------------------------------------------------------- #
# the reasoner
# --------------------------------------------------------------------------- #
class GroundedReasoner:
    """All AI capabilities the command centre exposes, each with a fallback."""

    def __init__(self, client: OllamaClient | None = None) -> None:
        self.client = client or OllamaClient()
        self.stats = {"calls": 0, "accepted": 0, "rejected": 0, "fallbacks": 0, "errors": 0}

    # -- generic ---------------------------------------------------------- #
    async def _ask(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        grounded: Iterable[float | int | str],
        fallback: str,
        normalize: Any = None,
    ) -> AIResult:
        """Run one grounded call, or return the fallback. Never raises."""
        started = time.perf_counter()
        prompt_hash = hashlib.sha256(f"{task}:{prompt}".encode("utf-8")).hexdigest()[:16]
        self.stats["calls"] += 1
        if not self.client.available:
            self.stats["fallbacks"] += 1
            return AIResult(fallback, True, "deterministic", True, 0.0, prompt_hash=prompt_hash)
        try:
            body = await self.client.generate_json(prompt, system=system)
        except Exception as exc:  # noqa: BLE001 - the model is never allowed to break the EOC
            self.client._record_failure()
            self.stats["errors"] += 1
            return AIResult(fallback, True, "deterministic", True, round((time.perf_counter() - started) * 1000, 1), error=redact(str(exc)), prompt_hash=prompt_hash)
        text = str(body.get("text") or body.get("answer") or body.get("summary") or "").strip()
        if normalize is not None:
            try:
                text = normalize(body, text)
            except Exception:  # noqa: BLE001
                text = text
        latency = round((time.perf_counter() - started) * 1000, 1)
        if not text:
            self.stats["fallbacks"] += 1
            return AIResult(fallback, True, "deterministic", True, latency, error="empty model output", prompt_hash=prompt_hash)
        ungrounded = check_grounding(text, grounded)
        if ungrounded and settings.ai_require_grounding:
            self.stats["rejected"] += 1
            log.warning(
                "ai draft rejected: ungrounded figures",
                extra={"custos": {"task": task, "ungrounded": ungrounded[:8], "model": self.client.model}},
            )
            return AIResult(
                fallback,
                True,
                self.client.model,
                True,
                latency,
                ungrounded_numbers=ungrounded[:12],
                rejected_draft=text[:600],
                prompt_hash=prompt_hash,
            )
        self.stats["accepted"] += 1
        return AIResult(text, True, self.client.model, False, latency, prompt_hash=prompt_hash, tokens_out=len(text.split()))

    # -- capabilities ----------------------------------------------------- #
    async def brief_scenario(self, *, narrative: str, kpis: Mapping[str, Any], facts: Sequence[str]) -> AIResult:
        """Rewrite the deterministic brief. The template remains the source of truth."""
        grounded = collect_numbers(kpis) | collect_numbers(facts) | collect_numbers(narrative)
        system = (
            "You are the duty officer's briefing writer inside a city emergency operations centre. "
            "You may only restate figures present in the FACTS block. Never invent a number, never estimate, "
            "never soften a warning. Reply as JSON: {\"text\": \"<briefing>\"}."
        )
        prompt = (
            "FACTS (authoritative, from the simulation engines):\n"
            + "\n".join(f"- {fact}" for fact in facts)
            + "\n\nTEMPLATE BRIEF (already correct; rephrase for clarity, 5 sentences maximum):\n"
            + narrative
            + "\n\nReturn JSON only."
        )
        return await self._ask(task="brief_scenario", system=system, prompt=prompt, grounded=grounded, fallback=narrative)

    async def assess_anomaly(self, *, metric: str, value: float, baseline: float, spread: float, z: float, context: str) -> AIResult:
        """Classify a sensor excursion. Fallback is the z-score band, which is already defensible."""
        deterministic = self._anomaly_fallback(metric=metric, value=value, baseline=baseline, z=z, context=context)
        grounded = [value, baseline, spread, z, round(abs(z), 2)]
        system = (
            "You are a hydrology duty analyst. Classify the observation as one of "
            "sensor_fault, calibration_drift, genuine_flood_signal, infrastructure_failure, infiltration_or_leak, unknown. "
            "Justify using ONLY the supplied numbers. Reply as JSON: {\"text\": \"<one sentence>\", \"class\": \"<class>\"}."
        )
        prompt = (
            f"METRIC: {metric}\nVALUE: {value}\nBASELINE_MEDIAN: {baseline}\nROBUST_SIGMA: {spread}\nZ_SCORE: {z}\n"
            f"CONTEXT: {context}\n\nReturn JSON only."
        )
        result = await self._ask(
            task="assess_anomaly",
            system=system,
            prompt=prompt,
            grounded=grounded,
            fallback=deterministic,
            normalize=lambda body, text: f"{body.get('class', 'unknown')}: {text}" if body.get("class") else text,
        )
        return result

    @staticmethod
    def _anomaly_fallback(*, metric: str, value: float, baseline: float, z: float, context: str) -> str:
        magnitude = abs(z)
        if magnitude < 3.0:
            band = "within normal variation"
        elif magnitude < 5.0:
            band = "a notable excursion"
        elif magnitude < 8.0:
            band = "a strong excursion"
        else:
            band = "an extreme excursion"
        direction = "above" if value >= baseline else "below"
        # The deterministic classifier is not a placeholder: a robust z-score on a
        # median/MAD baseline is exactly what a hydrologist would compute first.
        verdict = "genuine_flood_signal" if (value > baseline and metric in {"water_level", "flood_depth", "rainfall_mm_h", "river_stage", "stage_m"}) else "sensor_fault"
        if abs(value - baseline) < max(0.02, 0.02 * abs(baseline)):
            verdict = "calibration_drift"
        return (
            f"{verdict}: {metric} reads {value:g} against a baseline of {baseline:g} "
            f"({band}, robust z={z:.1f}). {context}"
        )

    async def triage_incident(self, *, summary: str, context_facts: Sequence[str], kpis: Mapping[str, Any]) -> AIResult:
        grounded = collect_numbers(kpis) | collect_numbers(context_facts) | collect_numbers(summary)
        system = (
            "You are the triage officer in a city emergency operations centre. Recommend an action in one sentence "
            "using only the FACTS. Do not invent locations, assets or numbers. "
            "Reply as JSON: {\"text\": \"<recommendation>\", \"action\": \"<dispatch|monitor|escalate|verify>\"}."
        )
        prompt = (
            f"INCIDENT: {summary}\n\nFACTS:\n"
            + "\n".join(f"- {fact}" for fact in context_facts)
            + "\n\nReturn JSON only."
        )
        fallback = "verify: confirm the report against the twin before committing resources; " + (
            context_facts[0] if context_facts else "no further structured context available"
        )
        return await self._ask(
            task="triage_incident",
            system=system,
            prompt=prompt,
            grounded=grounded,
            fallback=fallback,
            normalize=lambda body, text: f"{body.get('action', 'verify')}: {text}" if body.get("action") else text,
        )

    async def choose_corridor(self, *, target: str, options: Sequence[Mapping[str, Any]]) -> AIResult:
        """Explain a routing choice already computed by the mobility engine."""
        if not options:
            return AIResult("no traversable corridor is available to this destination", True, "deterministic", True, 0.0)
        best = min(options, key=lambda o: float(o.get("eta_min", 1e9)))
        ordered = sorted(options, key=lambda o: float(o.get("eta_min", 1e9)))[:4]
        facts = [
            f"option {index + 1}: {o.get('name')} - eta {float(o.get('eta_min', 0)):.0f} min, "
            f"max depth on route {float(o.get('max_depth_m', 0)):.2f} m, status {o.get('status')}"
            for index, o in enumerate(ordered)
        ]
        fallback = (
            f"{best.get('name')} is the fastest usable corridor at {float(best.get('eta_min', 0)):.0f} min "
            f"(deepest water on that route {float(best.get('max_depth_m', 0)):.2f} m)."
        )
        grounded = collect_numbers(facts) | collect_numbers(ordered)
        system = (
            "You are a mobility planner. Recommend exactly one corridor from the OPTIONS and justify it with the "
            "supplied figures only. Reply as JSON: {\"text\": \"<one sentence>\"}."
        )
        prompt = f"DESTINATION: {target}\n\nOPTIONS:\n" + "\n".join(f"- {fact}" for fact in facts) + "\n\nReturn JSON only."
        return await self._ask(task="choose_corridor", system=system, prompt=prompt, grounded=grounded, fallback=fallback)

    async def explain_kpi(self, *, name: str, value: float, drivers: Sequence[str], evidence: Sequence[str]) -> AIResult:
        grounded = collect_numbers([value]) | collect_numbers(drivers) | collect_numbers(evidence)
        fallback = (
            f"{name} is {value:g} in the representative member of the ensemble. "
            + ("Primary drivers: " + "; ".join(drivers[:3]) + "." if drivers else "No dominant driver was identified.")
        )
        system = (
            "You explain one emergency-management indicator to a commander in two sentences. Use only the supplied "
            "figures and driver names. Reply as JSON: {\"text\": \"<explanation>\"}."
        )
        prompt = (
            f"INDICATOR: {name}\nVALUE: {value}\nDRIVERS:\n"
            + "\n".join(f"- {driver}" for driver in drivers)
            + "\nEVIDENCE:\n"
            + "\n".join(f"- {item}" for item in evidence[:10])
            + "\n\nReturn JSON only."
        )
        return await self._ask(task="explain_kpi", system=system, prompt=prompt, grounded=grounded, fallback=fallback)

    def snapshot_stats(self) -> dict[str, Any]:
        return dict(self.stats)


REASONER = GroundedReasoner()
