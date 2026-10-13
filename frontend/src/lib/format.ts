/**
 * Presentation helpers.
 *
 * Two rules run through this file, both of them about honesty:
 *
 * 1. **Never invent precision.** A KPI is rendered at the precision its
 *    uncertainty supports: 2 002 patients, not 2 001.5, and an ensemble band is
 *    always shown with the point estimate, because a single number from a
 *    stochastic model is a lie of omission.
 * 2. **Never hide staleness.** Age is always rendered, and it turns amber, then
 *    red. An operator reading a six-minute-old depth must know it is six minutes
 *    old without hovering.
 */

import type { KpiMap } from "./types";

export function formatNumber(value: number | null | undefined, kind: "count" | "float" | "compact" = "count"): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (kind === "count") return Math.round(value).toLocaleString("en-IN");
  if (kind === "compact") {
    const abs = Math.abs(value);
    if (abs >= 1e7) return `${(value / 1e7).toFixed(2)} cr`;
    if (abs >= 1e5) return `${(value / 1e5).toFixed(2)} lakh`;
    if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
    return Math.round(value).toString();
  }
  return value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function formatSigned(value: number | null | undefined, kind: "count" | "float" = "count"): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const rounded = kind === "count" ? Math.round(value) : Number(value.toFixed(2));
  if (rounded === 0) return "0";
  return `${rounded > 0 ? "+" : "−"}${formatNumber(Math.abs(rounded), kind)}`;
}

export function formatUnit(value: number | null | undefined, unit: string, kind?: "count" | "float" | "compact"): string {
  const text = formatNumber(value, kind ?? (unit === "people" || unit === "count" ? "count" : "float"));
  return unit ? `${text} ${unit}` : text;
}

export function formatDepth(metres: number | null | undefined): string {
  if (metres === null || metres === undefined) return "—";
  if (metres <= 0.005) return "dry";
  if (metres < 1) return `${Math.round(metres * 100)} cm`;
  return `${metres.toFixed(2)} m`;
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "unknown";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function formatAgeFrom(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  return formatAge((now - then) / 1000);
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function ageTone(seconds: number | null | undefined, warnS = 300, badS = 900): "ok" | "warn" | "bad" | "unknown" {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "unknown";
  if (seconds >= badS) return "bad";
  if (seconds >= warnS) return "warn";
  return "ok";
}

export function confidenceBand(confidence: number | null | undefined): "low" | "medium" | "high" | "unknown" {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) return "unknown";
  if (confidence >= 0.75) return "high";
  if (confidence >= 0.5) return "medium";
  return "low";
}

/** Asset status → tone class. Unknown states degrade loudly, never silently. */
export function statusTone(status: string | null | undefined): string {
  switch (status) {
    case "ok":
    case "normal":
      return "tone-ok";
    case "degraded":
    case "impaired":
      return "tone-warn";
    case "failed":
    case "severe":
    case "critical":
      return "tone-bad";
    case "offline":
    case "unknown":
      return "tone-mute";
    default:
      return "tone-warn";
  }
}

export function severityTone(severity: number): string {
  if (severity >= 4) return "tone-bad";
  if (severity >= 3) return "tone-warn";
  if (severity >= 2) return "tone-info";
  return "tone-mute";
}

export function depthTone(depth: number): string {
  if (depth >= 0.5) return "tone-bad";
  if (depth >= 0.15) return "tone-warn";
  if (depth > 0.02) return "tone-info";
  return "tone-mute";
}

export function titleise(text: string): string {
  return text
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

/**
 * A compact uncertainty band, e.g. `2,002 – 2,047 patients`.
 * Rendered *next to* the point estimate, never instead of it.
 */
export function formatBand(p10: number | undefined, p90: number | undefined, unit: string): string {
  if (p10 === undefined || p90 === undefined) return "";
  const kind = unit === "people" || unit === "%" ? "count" : "float";
  if (Math.abs(p90 - p10) < (kind === "count" ? 1 : 0.05)) return "no ensemble spread";
  return `${formatNumber(p10, kind)} – ${formatNumber(p90, kind)} ${unit}`.trim();
}

export function relativeDelta(value: number, baseline: number): number {
  if (!baseline) return 0;
  return ((value - baseline) / Math.abs(baseline)) * 100;
}

/** Which of two KPI maps is better, using the KPI dictionary's direction. */
export function betterSide(delta: number, lowerIsBetter: boolean): "a" | "b" | "neither" {
  if (Math.abs(delta) < 1e-9) return "neither";
  const improved = lowerIsBetter ? delta < 0 : delta > 0;
  return improved ? "b" : "a";
}

export function kpiEntries(kpis: KpiMap | null | undefined, order: string[]): [string, number][] {
  if (!kpis) return [];
  const ranked = order.filter((key) => key in kpis);
  const rest = Object.keys(kpis).filter((key) => !ranked.includes(key));
  return [...ranked, ...rest].map((key) => [key, kpis[key] as number]);
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function percent(part: number, whole: number): number {
  if (!whole) return 0;
  return (part / whole) * 100;
}
