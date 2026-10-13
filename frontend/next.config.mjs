/**
 * CUSTOS console build configuration.
 *
 * Two decisions worth stating out loud, because they are the difference between
 * a demo and something an EOC can run:
 *
 * 1. `output: "export"` — the console is a *static* bundle. The API serves it
 *    from the same origin at `/console`, so a deployment is one process, one
 *    port, and no CORS. An air-gapped control room has no CDN and often no
 *    reverse proxy, and a console that needs either is a console that is down.
 * 2. `basePath: "/console"` — every asset URL is prefixed, so the exact same
 *    bundle works under the API's mount, under `next dev`, and behind a path
 *    prefix in a hardened deployment.
 */
const nextConfig = {
  output: "export",
  basePath: "/console",
  trailingSlash: true,
  reactStrictMode: true,
  images: { unoptimized: true },
  // The dev server is only used for UI work (production is a static export served
  // by the API). In dev, relative `/api/v1/...` calls are proxied to the backend so
  // the browser never needs to know its port — and, importantly, the code path
  // exercised in dev is the same relative-path code path that runs in production.
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    const api = process.env.CUSTOS_API_ORIGIN || "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
};

export default nextConfig;
