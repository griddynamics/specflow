// The words of the Linear integration on the Integrations page, free of React: the one toast of the return route's `?linear=<code>` and what a
// failed connect or disconnect says by API code.
import { V2_COPY } from "../v2-copy";

const TOAST_FAILURE: Readonly<Record<string, string>> = {
  linear_consent_denied: V2_COPY.linearConsentDenied,
  linear_state_invalid: V2_COPY.linearSignInFailed,
  linear_code_invalid: V2_COPY.linearSignInFailed,
  linear_auth_failed: V2_COPY.linearUnreachable,
  linear_unreachable: V2_COPY.linearUnreachable,
  linear_unavailable: V2_COPY.linearUnavailable,
};

/** The one toast of `?linear=<code>` (the return route's answer): `connected` succeeds, any other value is a failure. */
export function linearToast(code: string): { title: string; tone: "success" | "danger" } {
  if (code === "connected") return { title: V2_COPY.connectedToast, tone: "success" };
  return { title: Object.hasOwn(TOAST_FAILURE, code) ? TOAST_FAILURE[code] : V2_COPY.actionFailed, tone: "danger" };
}

/** What a failed connect or disconnect of Linear says, by API code (merged into the page's table). */
export const LINEAR_FAILURE: Readonly<Record<string, string>> = {
  linear_not_connected: V2_COPY.notConnectedFirst,
  linear_reconnect_required: V2_COPY.linearSignInFailed,
  linear_unavailable: V2_COPY.linearUnavailable,
  linear_unreachable: V2_COPY.linearUnreachable,
};
