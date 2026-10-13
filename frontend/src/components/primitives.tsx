/**
 * Shared primitives.
 *
 * Deliberately small: the console's value is in what it *says*, not in a
 * component zoo. Everything here exists to make one of three things impossible —
 * an unsourced number, an unlabelled state, or a confidence that is not shown.
 */

"use client";

import type { ReactNode } from "react";

export function Chip({
  tone = "",
  title,
  children,
}: {
  tone?: string;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span className={`chip ${tone}`} title={title}>
      {children}
    </span>
  );
}

export function Panel({
  title,
  actions,
  children,
  foot,
  grow = true,
  bodyClass = "",
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  foot?: ReactNode;
  grow?: boolean;
  bodyClass?: string;
}) {
  return (
    <section className="panel" style={grow ? undefined : { flex: "0 0 auto" }}>
      <header className="panel-head">
        <span className="panel-title">{title}</span>
        <span className="grow" />
        {actions}
      </header>
      <div className={`panel-body ${bodyClass}`}>{children}</div>
      {foot ? <footer className="panel-foot">{foot}</footer> : null}
    </section>
  );
}

export function KpiCard({
  label,
  value,
  unit,
  band,
  delta,
  deltaLabel,
  tone,
  meterPct,
  hint,
}: {
  label: string;
  value: string;
  unit?: string;
  band?: string;
  delta?: string;
  deltaLabel?: string;
  tone?: string;
  meterPct?: number;
  hint?: string;
}) {
  return (
    <div className="kpi" title={hint}>
      <div className="kpi-label">
        <span>{label}</span>
        {delta ? (
          <span className={`kpi-delta ${tone ?? ""}`} title={deltaLabel}>
            {delta}
          </span>
        ) : null}
      </div>
      <div className={`kpi-value ${tone ?? ""}`}>
        {value}
        {unit ? <span className="small mute"> {unit}</span> : null}
      </div>
      {band ? <div className="kpi-band">{band}</div> : null}
      {meterPct !== undefined ? (
        <div className={`meter ${tone === "tone-bad" ? "bad" : tone === "tone-warn" ? "warn" : tone === "tone-ok" ? "ok" : ""}`}>
          <span style={{ width: `${Math.max(0, Math.min(100, meterPct))}%` }} />
        </div>
      ) : null}
    </div>
  );
}

export function ConfidenceBadge({ confidence, band }: { confidence: number | null | undefined; band?: string }) {
  const value = confidence ?? 0;
  const tone = value >= 0.75 ? "tone-ok" : value >= 0.5 ? "tone-warn" : "tone-bad";
  const label = band ?? (value >= 0.75 ? "high" : value >= 0.5 ? "medium" : "low");
  return (
    <Chip tone={tone} title="Model confidence: how much of the ensemble agrees on the headline figure.">
      <span className="dot" />
      confidence {value ? `${Math.round(value * 100)}%` : "unknown"} · {label}
    </Chip>
  );
}

export function FreshnessChip({ ageSeconds, label = "twin" }: { ageSeconds: number | null | undefined; label?: string }) {
  const tone = ageSeconds === null || ageSeconds === undefined ? "tone-mute" : ageSeconds > 900 ? "tone-bad" : ageSeconds > 300 ? "tone-warn" : "tone-ok";
  const text =
    ageSeconds === null || ageSeconds === undefined
      ? "age unknown"
      : ageSeconds < 60
        ? `${Math.round(ageSeconds)} s old`
        : `${Math.round(ageSeconds / 60)} min old`;
  return (
    <Chip tone={tone} title="Age of the data behind this view. Anything over 5 minutes is amber; over 15 minutes is red.">
      <span className="dot" />
      {label} {text}
    </Chip>
  );
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <div className="progress">
        <span style={{ width: `${Math.max(2, Math.min(100, value * 100))}%` }} />
      </div>
      {label ? <div className="tiny mute">{label}</div> : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className="switch" title={hint}>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

/** A labelled number with its unit and, when known, its source. */
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="field" title={hint}>
      <label>{label}</label>
      {children}
    </div>
  );
}
