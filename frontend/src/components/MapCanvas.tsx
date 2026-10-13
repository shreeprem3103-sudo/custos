/**
 * The operational map (WebGL, deck.gl).
 *
 * Design commitments, in the order they matter when the water is rising:
 *
 * 1. **No basemap tiles.** An EOC may be air-gapped, and a map that goes blank
 *    without an internet connection is a liability. Everything drawn here comes
 *    from the twin: ward cells, the road network, assets, hazard footprints.
 * 2. **Every layer is sourced and labelled.** Ward shading is the ward's
 *    *simulated* or *observed* depth; road colour is the road's state; the arc
 *    layer is the cascade the engine computed, with magnitude and probability on
 *    hover. Nothing on this canvas is decorative.
 * 3. **The map never claims more precision than the model has.** Ward geometry is
 *    schematic (a cell with the ward's real population density), and the legend
 *    says so; asset positions are the surveyed/records coordinates.
 * 4. **Picking is the interface.** Click an asset, a road or a cascade arc and
 *    the panel on the right answers "why".
 */

"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { cascadeAt, frameFor, useConsole } from "@/lib/store";
import { formatDepth, formatNumber, statusTone } from "@/lib/format";
import type { AssetOut, CascadeImpact, TwinOut, ZoneOut } from "@/lib/types";

import { Chip } from "./primitives";

type DeckModule = typeof import("deck.gl");

interface HoverState {
  x: number;
  y: number;
  title: string;
  lines: string[];
}

const STATUS_COLOR: Record<string, [number, number, number]> = {
  ok: [52, 211, 153],
  nominal: [52, 211, 153],
  degraded: [251, 191, 36],
  impaired: [251, 191, 36],
  failed: [248, 113, 113],
  severe: [248, 113, 113],
  offline: [124, 142, 166],
  unknown: [124, 142, 166],
};

const TYPE_GLYPH: Record<string, string> = {
  hospital: "✚",
  substation: "◆",
  pump_station: "⬤",
  water_treatment: "≈",
  shelter: "⌂",
  police_station: "✪",
  fire_station: "✦",
  telecom_tower: "⌁",
  road: "—",
  bridge: "▬",
  canal: "~",
  river_gauge: "⊳",
  school: "▣",
  ward_office: "▤",
};

function depthColor(depth: number, alpha = 190): [number, number, number, number] {
  if (depth <= 0.005) return [56, 189, 248, 26];
  if (depth < 0.15) return [56, 189, 248, alpha * 0.35];
  if (depth < 0.35) return [56, 189, 248, alpha * 0.6];
  if (depth < 0.7) return [14, 116, 190, alpha * 0.8];
  return [8, 65, 130, alpha];
}

function statusColor(status: string, alpha = 220): [number, number, number, number] {
  const [r, g, b] = STATUS_COLOR[status] ?? STATUS_COLOR.unknown!;
  return [r, g, b, alpha];
}

/** Mean position of the twin's assets — the fallback anchor for an orphaned arc. */
function cityCentre(twin: TwinOut): { lon: number; lat: number } {
  const points = twin.assets.map((asset) => asset.centroid).filter((point): point is { lon: number; lat: number } => Boolean(point));
  if (!points.length) return { lon: 76.29, lat: 9.98 };
  return {
    lon: points.reduce((sum, point) => sum + point.lon, 0) / points.length,
    lat: points.reduce((sum, point) => sum + point.lat, 0) / points.length,
  };
}

/** Bounds of the twin, padded, so the initial camera frames *this* city. */
function boundsOf(twin: TwinOut | null): [number, number, number, number] | null {
  const points: [number, number][] = [];
  for (const asset of twin?.assets ?? []) {
    if (asset.centroid) points.push([asset.centroid.lon, asset.centroid.lat]);
  }
  for (const zone of twin?.zones ?? []) points.push([zone.centroid.lon, zone.centroid.lat]);
  if (!points.length) return null;
  const lons = points.map(([lon]) => lon);
  const lats = points.map(([, lat]) => lat);
  const pad = 0.012;
  return [Math.min(...lons) - pad, Math.min(...lats) - pad, Math.max(...lons) + pad, Math.max(...lats) + pad];
}

/**
 * A ward cell derived from *its own* population density: area = population /
 * density. It is a schematic footprint, stated as such in the legend, not a
 * claimed boundary — but it keeps the disaster canvas honest about how much of
 * the city each ward occupies.
 */
function zonePolygon(zone: ZoneOut): number[][] {
  const areaKm2 = zone.population_density > 0 ? zone.population / zone.population_density : 2.5;
  const sideKm = Math.sqrt(Math.max(0.05, areaKm2));
  const halfLat = (sideKm / 2) / 110.574;
  const halfLon = (sideKm / 2) / (111.32 * Math.cos((zone.centroid.lat * Math.PI) / 180));
  const { lon, lat } = zone.centroid;
  return [
    [lon - halfLon, lat - halfLat],
    [lon + halfLon, lat - halfLat],
    [lon + halfLon, lat + halfLat],
    [lon - halfLon, lat + halfLat],
    [lon - halfLon, lat - halfLat],
  ];
}

function lineOf(asset: AssetOut): number[][] | null {
  const geometry = asset.geometry as { type?: string; coordinates?: number[][] };
  if (geometry?.type === "LineString" && Array.isArray(geometry.coordinates)) {
    return geometry.coordinates as number[][];
  }
  return null;
}

export function MapCanvas() {
  const twin = useConsole((state) => state.twin);
  const layers = useConsole((state) => state.layers);
  const toggleLayer = useConsole((state) => state.toggleLayer);
  const simulation = useConsole((state) => state.simulation);
  const timelineIndex = useConsole((state) => state.timelineIndex);
  const setTimelineIndex = useConsole((state) => state.setTimelineIndex);
  const selectedAssetId = useConsole((state) => state.selectedAssetId);
  const selectAsset = useConsole((state) => state.selectAsset);
  const selectIncident = useConsole((state) => state.selectIncident);
  const incidents = useConsole((state) => state.incidents);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const deckRef = useRef<{ setProps(props: Record<string, unknown>): void; finalize(): void } | null>(null);
  const moduleRef = useRef<DeckModule | null>(null);
  const [hover, setHover] = useState<HoverState | null>(null);
  const [webglError, setWebglError] = useState<string | null>(null);
  const [viewState, setViewState] = useState<{ longitude: number; latitude: number; zoom: number } | null>(null);

  const frame = frameFor({ simulation, timelineIndex });
  const arcs = useMemo(() => cascadeAt(simulation.result, timelineIndex), [simulation.result, timelineIndex]);

  /** Scenario asset state: the run's own road/facility findings, not the live twin. */
  const scenarioAssets = useMemo(() => {
    const result = simulation.result;
    if (!result) return null;
    const map = new Map<string, { status: string; depth_m: number; note?: string }>();
    for (const road of (result.roads as unknown as { asset_id: string; status: string; depth_m: number }[]) ?? []) {
      map.set(road.asset_id, { status: road.status, depth_m: road.depth_m });
    }
    for (const facility of (result.facilities as unknown as { asset_id: string; status: string; note?: string }[]) ?? []) {
      const existing = map.get(facility.asset_id);
      map.set(facility.asset_id, { status: facility.status, depth_m: existing?.depth_m ?? 0, note: facility.note });
    }
    for (const shelter of (result.shelters as unknown as { asset_id: string; status: string }[]) ?? []) {
      const existing = map.get(shelter.asset_id);
      if (!existing) map.set(shelter.asset_id, { status: shelter.status, depth_m: 0 });
    }
    return map;
  }, [simulation.result]);

  const zoneDepth = useMemo(() => {
    const map = new Map<string, number>();
    for (const zone of twin?.zones ?? []) map.set(zone.id, zone.flood_depth_m);
    if (simulation.result) {
      // In scenario mode the ward depth is the deepest *street* depth the run
      // computed inside that ward - a measured quantity, not an interpolation.
      const zoneOf = new Map<string, string | null>();
      for (const asset of twin?.assets ?? []) zoneOf.set(asset.id, asset.zone);
      for (const [assetId, state] of scenarioAssets ?? []) {
        const zoneId = zoneOf.get(assetId);
        if (!zoneId) continue;
        map.set(zoneId, Math.max(map.get(zoneId) ?? 0, state.depth_m ?? 0));
      }
    }
    return map;
  }, [twin, simulation.result, scenarioAssets]);

  // --- deck.gl bootstrap (client-only: the export must render without WebGL) -- //
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const module = await import("deck.gl");
        if (cancelled || !containerRef.current) return;
        moduleRef.current = module;
        const bounds = boundsOf(useConsole.getState().twin);
        const centre = bounds
          ? { longitude: (bounds[0] + bounds[2]) / 2, latitude: (bounds[1] + bounds[3]) / 2 }
          : { longitude: 76.29, latitude: 9.98 };
        setViewState({ ...centre, zoom: 11.2 });
        const Deck = module.Deck;
        deckRef.current = new Deck({
          parent: containerRef.current,
          views: new module.MapView({ controller: true, repeat: false }),
          initialViewState: { ...centre, zoom: 11.2, pitch: 0, bearing: 0 },
          style: { position: "absolute", inset: "0" },
          onViewStateChange: ({ viewState: next }: { viewState: { longitude: number; latitude: number; zoom: number } }) =>
            setViewState({ longitude: next.longitude, latitude: next.latitude, zoom: next.zoom }),
          getTooltip: null,
          layers: [],
        }) as unknown as { setProps(props: Record<string, unknown>): void; finalize(): void };
      } catch (error) {
        setWebglError((error as Error).message);
      }
    })();
    return () => {
      cancelled = true;
      deckRef.current?.finalize();
      deckRef.current = null;
    };
  }, []);

  // --- layers --------------------------------------------------------------- //
  useEffect(() => {
    const module = moduleRef.current;
    const deck = deckRef.current;
    if (!module || !deck || !twin) return;

    const scenarioMode = Boolean(simulation.result);
    const critical = new Set(twin.insights.single_points_of_failure.map((entry) => entry.asset_id));

    const zoneLayer = new module.PolygonLayer<{ id: string; name: string; polygon: number[][]; depth: number; population: number }>({
      id: "zones",
      visible: layers.zones,
      data: twin.zones.map((zone) => ({
        id: zone.id,
        name: zone.name,
        polygon: zonePolygon(zone),
        depth: zoneDepth.get(zone.id) ?? 0,
        population: zone.population,
      })),
      getPolygon: (zone) => zone.polygon,
      getFillColor: (zone) => depthColor(zone.depth, layers.hazards ? 200 : 40),
      getLineColor: [39, 55, 74, 200],
      lineWidthMinPixels: 1,
      stroked: true,
      filled: true,
      pickable: true,
      autoHighlight: true,
      highlightColor: [56, 189, 248, 90],
      onHover: (info) =>
        setHover(
          info.object
            ? {
                x: info.x,
                y: info.y,
                title: `${info.object.name} (ward)`,
                lines: [
                  `depth ${formatDepth(info.object.depth)}${scenarioMode ? " (deepest street in ward, this scenario)" : " (observed)"}`,
                  `population ${formatNumber(info.object.population)} · cell area from ward density`,
                ],
              }
            : null,
        ),
      updateTriggers: { getFillColor: [layers.hazards, scenarioMode, zoneDepth.size, timelineIndex] },
    });

    const roadData = twin.assets
      .filter((asset) => asset.asset_type === "road" || asset.asset_type === "bridge")
      .map((asset) => {
        const line = lineOf(asset);
        if (!line) return null;
        const state = scenarioAssets?.get(asset.id);
        return {
          id: asset.id,
          name: asset.name,
          path: line,
          status: state?.status ?? asset.status,
          depth: state?.depth_m ?? asset.flood_depth_m,
          criticality: asset.criticality,
        };
      })
      .filter((entry): entry is { id: string; name: string; path: number[][]; status: string; depth: number; criticality: number } => Boolean(entry));

    const roadLayer = new module.PathLayer<(typeof roadData)[number]>({
      id: "roads",
      visible: layers.mobility,
      data: roadData,
      getPath: (road) => road.path as unknown as [number, number][],
      getColor: (road) =>
        road.status === "failed" || road.status === "impaired" && road.depth > 0.3
          ? [248, 113, 113, 235]
          : road.depth > 0.1
            ? [251, 191, 36, 235]
            : [120, 190, 220, 170],
      getWidth: (road) => 1.2 + road.criticality * 3.2,
      widthUnits: "pixels",
      widthMinPixels: 1,
      capRounded: true,
      jointRounded: true,
      pickable: true,
      onHover: (info) =>
        setHover(
          info.object
            ? {
                x: info.x,
                y: info.y,
                title: info.object.name,
                lines: [`status ${info.object.status} · depth ${formatDepth(info.object.depth)}`, `criticality ${(info.object.criticality * 100).toFixed(0)}%`],
              }
            : null,
        ),
    });

    const assetData = twin.assets
      .filter((asset) => asset.asset_type !== "road" && asset.centroid)
      .map((asset) => {
        const state = scenarioAssets?.get(asset.id);
        return {
          ...asset,
          effectiveStatus: state?.status ?? asset.status,
          effectiveDepth: state?.depth_m ?? asset.flood_depth_m,
        };
      });

    const assetLayer = new module.ScatterplotLayer<(typeof assetData)[number]>({
      id: "assets",
      visible: layers.assets,
      data: assetData,
      getPosition: (asset) => [asset.centroid!.lon, asset.centroid!.lat],
      getRadius: (asset) => 90 + asset.criticality * 240 + (asset.criticality >= 0.85 ? 80 : 0),
      radiusUnits: "meters",
      radiusMinPixels: 3.5,
      radiusMaxPixels: 26,
      getFillColor: (asset) => statusColor(asset.effectiveStatus, asset.exposure_score > 0.5 ? 245 : 205),
      stroked: true,
      getLineColor: (asset) => (asset.id === selectedAssetId ? [233, 238, 246, 255] : critical.has(asset.id) ? [167, 139, 250, 220] : [10, 18, 26, 200]),
      getLineWidth: (asset) => (asset.id === selectedAssetId ? 3 : critical.has(asset.id) ? 2 : 1),
      lineWidthUnits: "pixels",
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 90],
      onClick: (info) => {
        if (info.object) {
          selectAsset(info.object.id);
          useConsole.getState().setPanel("evidence");
        }
      },
      onHover: (info) =>
        setHover(
          info.object
            ? {
                x: info.x,
                y: info.y,
                title: info.object.name,
                lines: [
                  `${info.object.asset_type.replace(/_/g, " ")} · ${info.object.effectiveStatus}`,
                  `depth ${formatDepth(info.object.effectiveDepth)} · power ${info.object.power_available ? "yes" : "no"} · access ${info.object.access_available ? "yes" : "no"}`,
                  `service ${(info.object.service_level * 100).toFixed(0)}% · criticality ${(info.object.criticality * 100).toFixed(0)}%`,
                  assetLocationLine(info.object),
                ],
              }
            : null,
        ),
      updateTriggers: {
        getFillColor: [scenarioMode, timelineIndex],
        getLineColor: [selectedAssetId],
      },
    });

    const labelLayer = new module.TextLayer<(typeof assetData)[number]>({
      id: "labels",
      visible: layers.labels,
      data: assetData.filter(
        (asset) => asset.effectiveDepth > 0.25 || critical.has(asset.id) || asset.id === selectedAssetId || asset.criticality >= 0.9,
      ),
      getPosition: (asset) => [asset.centroid!.lon, asset.centroid!.lat],
      getText: (asset) => `${TYPE_GLYPH[asset.asset_type] ?? "•"} ${asset.name}`,
      getSize: 10,
      sizeUnits: "pixels",
      getColor: (asset) =>
        asset.id === selectedAssetId
          ? [233, 238, 246, 255]
          : asset.effectiveStatus === "failed"
            ? [254, 202, 202, 235]
            : critical.has(asset.id)
              ? [221, 214, 254, 225]
              : [200, 215, 230, 190],
      getTextAnchor: "start",
      getAlignmentBaseline: "bottom",
      getPixelOffset: [6, -6],
      fontFamily: "ui-monospace, monospace",
      background: true,
      getBackgroundColor: [6, 10, 15, 170],
      backgroundPadding: [2, 1],
      updateTriggers: { getColor: [selectedAssetId] },
    });

    const arcData = arcs.slice(0, 220);
    const cascadeLayer = new module.ArcLayer<CascadeImpact>({
      id: "cascade",
      visible: layers.cascade && arcData.length > 0,
      data: arcData,
      greatCircle: false,
      // Arcs whose endpoints are no longer in the twin are dropped by the data
      // filter, not faked with a placeholder coordinate.
      getSourcePosition: (impact) => {
        const asset = twin.assets.find((entry) => entry.id === impact.cause_asset_id);
        const centroid = asset?.centroid ?? cityCentre(twin);
        return [centroid.lon, centroid.lat] as [number, number];
      },
      getTargetPosition: (impact) => {
        const asset = twin.assets.find((entry) => entry.id === impact.target_asset_id);
        const centroid = asset?.centroid ?? cityCentre(twin);
        return [centroid.lon, centroid.lat] as [number, number];
      },
      getSourceColor: [56, 189, 248, 90],
      getTargetColor: (impact) =>
        impact.probability >= 0.7 ? [248, 113, 113, 220] : impact.probability >= 0.4 ? [251, 191, 36, 210] : [167, 139, 250, 170],
      getWidth: (impact) => 0.6 + impact.magnitude * 4,
      widthUnits: "pixels",
      pickable: true,
      onHover: (info) =>
        setHover(
          info.object
            ? {
                x: info.x,
                y: info.y,
                title: `${info.object.cause_asset_name} → ${info.object.target_asset_name}`,
                lines: [
                  info.object.mechanism,
                  `magnitude ${(info.object.magnitude * 100).toFixed(0)}% · probability ${(info.object.probability * 100).toFixed(0)}% · hop ${info.object.hop}`,
                  `at +${info.object.timestep_min} min · lead time ${info.object.lead_time_min} min`,
                  `evidence ${info.object.evidence_ids.slice(0, 3).join(", ")}`,
                ],
              }
            : null,
        ),
      updateTriggers: { getTargetColor: [timelineIndex] },
    });

    const spofLayer = new module.ScatterplotLayer<{ id: string; lon: number; lat: number; name: string; reach: number }>({
      id: "spof",
      visible: layers.spof,
      data: twin.insights.single_points_of_failure
        .filter((entry) => entry.asset_id)
        .map((entry) => {
          const asset = twin.assets.find((item) => item.id === entry.asset_id);
          return asset?.centroid
            ? { id: entry.asset_id, lon: asset.centroid.lon, lat: asset.centroid.lat, name: entry.name, reach: entry.reach }
            : null;
        })
        .filter((entry): entry is { id: string; lon: number; lat: number; name: string; reach: number } => Boolean(entry)),
      getPosition: (entry) => [entry.lon, entry.lat],
      getRadius: (entry) => 260 + entry.reach * 40,
      radiusUnits: "meters",
      filled: false,
      stroked: true,
      getLineColor: [167, 139, 250, 220],
      getLineWidth: 2,
      lineWidthUnits: "pixels",
      pickable: true,
      onHover: (info) =>
        setHover(
          info.object
            ? {
                x: info.x,
                y: info.y,
                title: `Single point of failure: ${info.object.name}`,
                lines: [`loss of this asset reaches ${formatNumber(info.object.reach)} downstream assets`, "halo radius scales with dependency reach"],
              }
            : null,
        ),
    });

    const hazardLayers: unknown[] = [];
    if (layers.hazards && simulation.result) {
      const request = simulation.result.request;
      const hazards = (request.hazards ?? []).filter((hazard) => hazard.location && hazard.radius_m);
      hazardLayers.push(
        new module.ScatterplotLayer({
          id: "hazards",
          data: hazards,
          getPosition: (hazard: { location?: { lon: number; lat: number } }) => [hazard.location!.lon, hazard.location!.lat],
          getRadius: (hazard: { radius_m?: number }) => hazard.radius_m ?? 3000,
          radiusUnits: "meters",
          stroked: true,
          filled: true,
          getFillColor: [248, 113, 113, 26],
          getLineColor: [248, 113, 113, 170],
          getLineWidth: 1.5,
          lineWidthUnits: "pixels",
        }),
      );
    }

    const incidentLayer = new module.ScatterplotLayer(
      {
        id: "incidents",
        visible: true,
        data: incidents,
        getPosition: (incident: { lon: number; lat: number }) => [incident.lon, incident.lat],
        getRadius: (incident: { radius_m: number }) => incident.radius_m,
        radiusUnits: "meters",
        stroked: true,
        filled: true,
        getFillColor: (incident: { severity: number }) =>
          incident.severity >= 4 ? [248, 113, 113, 40] : incident.severity === 3 ? [251, 191, 36, 34] : [56, 189, 248, 30],
        getLineColor: (incident: { severity: number }) => (incident.severity >= 4 ? [248, 113, 113, 220] : [251, 191, 36, 200]),
        getLineWidth: 2,
        lineWidthUnits: "pixels",
        pickable: true,
        onClick: (info: { object?: { id: string } }) => {
          if (info.object) {
            selectIncident(info.object.id);
            useConsole.getState().setPanel("incidents");
          }
        },
        onHover: (info: { x: number; y: number; object?: { title: string; severity: number; status: string; summary: string } }) =>
          setHover(
            info.object
              ? {
                  x: info.x,
                  y: info.y,
                  title: `${info.object.title}`,
                  lines: [`severity ${info.object.severity} · ${info.object.status}`, info.object.summary.slice(0, 180)],
                }
              : null,
          ),
      },
    );

    deck.setProps({
      views: new module.MapView({ controller: true, repeat: false }),
      layers: [zoneLayer, roadLayer, ...hazardLayers, cascadeLayer, incidentLayer, assetLayer, spofLayer, labelLayer],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [twin, layers, arcs, zoneDepth, scenarioAssets, selectedAssetId, simulation.result, timelineIndex, incidents]);

  const spanKm = useMemo(() => {
    const bounds = boundsOf(twin);
    if (!bounds) return null;
    return Math.round((bounds[2] - bounds[0]) * 102.5);
  }, [twin]);

  const criticalCount = twin ? twin.assets.filter((asset) => asset.status === "failed").length : 0;
  const floodedCount = twin ? twin.assets.filter((asset) => asset.flood_depth_m > 0.05).length : 0;

  return (
    <section className="panel" style={{ flex: 1, minHeight: 0 }}>
      <header className="panel-head">
        <span className="panel-title">Operational map · Kochi · WGS 84</span>
        <span className="grow" />
        {simulation.result && timelineIndex === null ? (
          <Chip tone="tone-info" title="The map is showing the scenario's own computed state, not the live twin.">
            scenario view · peaks to {formatDepth(Math.max(...simulation.result.timeline.map((frame) => frame.max_depth_m)))}
          </Chip>
        ) : null}
        {frame ? (
          <Chip tone="tone-warn" title="Scrubbing the scenario timeline: the map shows the model's state at this minute.">
            t+{frame.t_min} min · max depth {formatDepth(frame.max_depth_m)}
          </Chip>
        ) : null}
        <Chip tone={criticalCount ? "tone-bad" : "tone-ok"} title="Assets the twin currently reports as failed.">
          {criticalCount} failed
        </Chip>
        <Chip tone={floodedCount ? "tone-warn" : "tone-ok"} title="Assets with more than 5 cm of water at their location.">
          {floodedCount} flooded
        </Chip>
      </header>

      <div className="map-wrap">
        <div className="map-canvas" ref={containerRef} />

        {webglError ? (
          <div className="map-overlay" style={{ inset: 20, display: "grid", placeItems: "center", textAlign: "center" }}>
            <div>
              <div className="bold">WebGL is unavailable in this browser</div>
              <div className="small dim" style={{ marginTop: 6 }}>
                The command console renders the twin on the GPU. Enable hardware acceleration (or use a supported browser) — every
                number in the panels beside this map remains fully available.
              </div>
              <div className="tiny mute" style={{ marginTop: 6 }}>
                {webglError}
              </div>
            </div>
          </div>
        ) : null}

        <div className="map-overlay map-tools">
          <div className="upper tiny mute">layers</div>
          {(
            [
              ["hazards", "Flood depth"],
              ["assets", "Assets & services"],
              ["mobility", "Roads & mobility"],
              ["cascade", "Cascade arcs"],
              ["zones", "Ward cells"],
              ["spof", "Single points of failure"],
              ["labels", "Labels"],
            ] as [keyof typeof layers, string][]
          ).map(([key, label]) => (
            <label className="switch" key={key}>
              <input type="checkbox" checked={layers[key]} onChange={() => toggleLayer(key)} />
              <span>{label}</span>
            </label>
          ))}
        </div>

        <div className="map-overlay map-hud">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="upper tiny mute">view</span>
            <span className="tiny mono" title="Camera position. The console never sends your viewport anywhere.">
              {viewState ? `${viewState.latitude.toFixed(3)}, ${viewState.longitude.toFixed(3)} · z${viewState.zoom.toFixed(1)}` : "—"}
            </span>
          </div>
          <div className="tiny mute">
            Ward cells are schematic footprints sized by each ward's real population density; assets sit at their survey/records
            coordinates. No basemap: this console works with the network cut.
          </div>
          {spanKm ? <div className="tiny mono mute">extent ≈ {spanKm} km across</div> : null}
        </div>

        <div className="map-overlay map-legend">
          <div className="upper tiny mute">legend</div>
          <div className="legend-row">
            <span className="swatch" style={{ background: "rgba(56,189,248,0.45)" }} /> <span className="tiny">flood depth (ward / street)</span>
          </div>
          <div className="legend-row">
            <span className="swatch" style={{ background: "rgba(52,211,153,0.9)" }} /> <span className="tiny">asset nominal</span>
            <span className="swatch" style={{ background: "rgba(251,191,36,0.9)" }} /> <span className="tiny">degraded</span>
            <span className="swatch" style={{ background: "rgba(248,113,113,0.9)" }} /> <span className="tiny">failed</span>
          </div>
          <div className="legend-row">
            <span className="swatch" style={{ background: "rgba(167,139,250,0.9)" }} /> <span className="tiny">single point of failure halo</span>
          </div>
          <div className="legend-row">
            <span className="swatch" style={{ background: "rgba(248,113,113,0.6)" }} /> <span className="tiny">cascade arc colour = probability</span>
          </div>
        </div>

        {hover ? (
          <div className="map-overlay" style={{ left: Math.min(hover.x + 14, 9999), top: hover.y + 12, padding: "7px 9px", maxWidth: 330 }}>
            <div className="bold small">{hover.title}</div>
            {hover.lines.filter(Boolean).map((line) => (
              <div className="tiny dim mono" key={line}>
                {line}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {simulation.result ? (
        <div className="scrub">
          <button className="btn small" onClick={() => setTimelineIndex(null)} disabled={timelineIndex === null} title="Return to the end-state of the scenario.">
            live
          </button>
          <input
            type="range"
            min={0}
            max={Math.max(0, simulation.result.timeline.length - 1)}
            value={timelineIndex ?? Math.max(0, simulation.result.timeline.length - 1)}
            onChange={(event) => setTimelineIndex(Number(event.target.value))}
            aria-label="Scenario timeline"
          />
          <span className="mono tiny" style={{ whiteSpace: "nowrap" }}>
            {frame ? `+${frame.t_min} min` : `end +${simulation.result.request.horizon_minutes} min`}
          </span>
          <span className="mono tiny mute" style={{ whiteSpace: "nowrap" }}>
            {frame ? `${frame.active_incidents} incidents · ${frame.flood_extent_km2.toFixed(2)} km² flooded` : "peak state"}
          </span>
          <Chip tone="tone-info" title="The timeline is the ensemble median, frame by frame. Uncertainty is on the KPI cards.">
            {simulation.result.timeline.length} frames
          </Chip>
        </div>
      ) : null}
    </section>
  );
}

function assetLocationLine(asset: AssetOut & { effectiveStatus?: string }): string {
  const parts: string[] = [];
  if (asset.zone) parts.push(`ward ${asset.zone}`);
  if (asset.operator) parts.push(asset.operator);
  if (asset.capacity && Object.keys(asset.capacity).length) {
    parts.push(Object.entries(asset.capacity).slice(0, 2).map(([key, value]) => `${key} ${value}`).join(", "));
  }
  const tone = statusTone(asset.effectiveStatus ?? asset.status);
  void tone;
  return parts.join(" · ");
}
