import path from "node:path";
import type { NextConfig } from "next";

// The npm workspace root. Set explicitly: a worktree sits inside another checkout whose lockfile Next would find first.
const workspaceRoot = path.join(__dirname, "..");

const nextConfig: NextConfig = {
  // A static site: `next build` writes out/ (docs: guides/static-exports.md). No server, no route handlers, no API calls.
  output: "export",
  poweredByHeader: false,
  transpilePackages: ["@graphico/ui"],
  turbopack: { root: workspaceRoot },
};

export default nextConfig;
