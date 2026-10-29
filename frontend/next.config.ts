import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server for the `runtime` image (docs: next-config-js/output.md).
  output: "standalone",
  poweredByHeader: false,
  // The dev container starts `next dev` with --hostname 0.0.0.0, so only
  // `localhost` may load dev assets by default; this lets the browser use
  // http://127.0.0.1:3000 too, the address compose publishes.
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
