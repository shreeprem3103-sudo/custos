/**
 * CUSTOS mark.
 *
 * The mark is a *shield drawn as a city cell* around a flood line: the same
 * geometry the console renders as a ward, with the water surface crossing it.
 * Inline SVG rather than a raster asset, because an air-gapped console has no
 * asset CDN, and because it must stay legible at 20 px in the title bar and
 * 96 px on a briefing cover.
 */

"use client";

export function Logo({ size = 30, withWordmark = false }: { size?: number; withWordmark?: boolean }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
      <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label="CUSTOS">
        <defs>
          <linearGradient id="custos-water" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.85" />
            <stop offset="100%" stopColor="#0e7fb8" stopOpacity="0.35" />
          </linearGradient>
        </defs>
        {/* the ward cell */}
        <path
          d="M32 4 L56 14 V34 C56 46 45 56 32 60 C19 56 8 46 8 34 V14 Z"
          fill="#0a141d"
          stroke="#2b4a63"
          strokeWidth="2.5"
        />
        {/* the flood surface */}
        <path d="M8 36 C16 31 24 40 32 35 C40 30 48 39 56 34 V40 C48 45 40 36 32 41 C24 46 16 37 8 42 Z" fill="url(#custos-water)" />
        {/* the sensor network inside the cell */}
        <g stroke="#38bdf8" strokeWidth="1.6" opacity="0.85">
          <line x1="20" y1="20" x2="32" y2="26" />
          <line x1="32" y1="26" x2="44" y2="19" />
          <line x1="32" y1="26" x2="32" y2="45" />
        </g>
        <g fill="#e9eef6">
          <circle cx="20" cy="20" r="3" />
          <circle cx="44" cy="19" r="3" />
        </g>
        <circle cx="32" cy="26" r="3.6" fill="#34d399" />
      </svg>
      {withWordmark ? (
        <span style={{ display: "grid", lineHeight: 1.05 }}>
          <span className="brand-name">CUSTOS</span>
          <span className="brand-sub">CRISIS DIGITAL TWIN</span>
        </span>
      ) : null}
    </span>
  );
}
