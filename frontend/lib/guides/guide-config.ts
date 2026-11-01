// MCP-M10 (E11): the values the Guides pages pass on as plain props. Server-only (it reads `appConfig()`): client modules never import it.
import { appConfig } from "@/lib/config";
import type { GuideMode } from "./snippets";

export interface GuideConfig {
  mode: GuideMode;
  mcpUrl: string;
  clientId: string;
  devEmail: string;
}

/** The variant follows the web app's own auth mode: `dev` -> the local variant, else production (E4). */
export function guideConfig(): GuideConfig {
  const cfg = appConfig();
  return { mode: cfg.authMode, mcpUrl: cfg.mcp.publicUrl, clientId: cfg.mcp.clientId, devEmail: cfg.localTestUserEmail };
}
