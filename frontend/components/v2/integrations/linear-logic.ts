// The words of the Linear integration on the Integrations page, free of React. FOUNDATION STUB: stream S1 fills the codes of its return route
// (`?linear=<code>`) and the API codes of a failed connect.
import { V2_COPY } from "../v2-copy";

/** The one toast of `?linear=<code>` (the return route's answer): `connected` succeeds, any other value is a failure. */
export function linearToast(code: string): { title: string; tone: "success" | "danger" } {
  if (code === "connected") return { title: V2_COPY.connectedToast, tone: "success" };
  return { title: V2_COPY.actionFailed, tone: "danger" };
}

/** What a failed connect or disconnect of Linear says, by API code (merged into the page's table). */
export const LINEAR_FAILURE: Readonly<Record<string, string>> = {
  linear_not_connected: V2_COPY.notConnectedFirst,
};
