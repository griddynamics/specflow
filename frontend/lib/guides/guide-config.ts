// MCP-M10 (E11): the values the Guides pages pass on as plain props. Server-only (it reads `appConfig()`): client modules never import it.
import { appConfig } from "@/lib/config";

export interface GuideConfig {
  mcpUrl: string;
  clientId: string;
  /** The name `claude mcp add` registers the server under (`MCP_SERVER_NAME`). */
  serverName: string;
}

export function guideConfig(): GuideConfig {
  const { mcp } = appConfig();
  return { mcpUrl: mcp.publicUrl, clientId: mcp.clientId, serverName: mcp.serverName };
}
