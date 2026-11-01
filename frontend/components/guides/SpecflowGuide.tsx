// MCP-M10: the SpecFlow 101 page body (server): header and the selector.
import { FLOW_101 } from "@/lib/guides/content";
import { routesFor } from "@/lib/routes";
import { Flow101 } from "./Flow101";
import { GuideHeader } from "./GuideHeader";
import "./guides.css";

export function SpecflowGuide() {
  return (
    <div className="guides" data-testid="guide-101">
      <GuideHeader title={FLOW_101.title} lead={FLOW_101.lead} />
      <Flow101 connectHref={routesFor("production").guides("mcp-server")} />
    </div>
  );
}
