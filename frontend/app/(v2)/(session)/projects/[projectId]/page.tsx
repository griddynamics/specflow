// AC1: `/projects/<id>` is the canvas of the project (D10). The server page only reads the MCP address the deployment advertises (the Guides'
// values) and hands it on as plain props; the project gate is the layout's.
import { Suspense } from "react";
import { CanvasScreen } from "@/components/v2/canvas/CanvasScreen";
import { guideConfig } from "@/lib/guides/guide-config";

export default function Page() {
  return (
    <Suspense>
      <CanvasScreen mcp={guideConfig()} />
    </Suspense>
  );
}
