// plans/LINEAR/LINEAR-PLAN.md, stream S1: the production `LinearPort` over the caller's Linear connection (`/integrations/linear`). Every body is
// shape-checked (like the GitHub port): any other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code.
// The consent URL the browser follows must be `https:` on linear.app.
import { authorizeLinear, getLinearConnection } from "@/lib/api";
import type { LinearConnection, LinearConnectionStatus, LinearPort } from "@/lib/domain/linear";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATUSES: readonly LinearConnectionStatus[] = ["none", "active", "expired", "revoked"];
const LINEAR_HOST = "linear.app";

const nonEmpty = (v: unknown): string => {
  expectShape(isString(v) && v !== "");
  return v;
};

/** An `https:` URL on linear.app; anything else is not the contract. */
function linearUrl(v: unknown): string {
  expectShape(isString(v) && URL.canParse(v));
  const url = new URL(v);
  expectShape(url.protocol === "https:" && url.hostname === LINEAR_HOST);
  return url.href;
}

export function toLinearConnection(v: unknown): LinearConnection {
  expectShape(isRecord(v) && typeof v.available === "boolean");
  const status = STATUSES.find((s) => s === v.status);
  expectShape(status !== undefined);
  if (status === "none") {
    expectShape(v.user_name === null && v.workspace === null);
    return { available: v.available, status, userName: null, workspace: null };
  }
  const workspace = v.workspace;
  expectShape(isRecord(workspace));
  return {
    available: v.available,
    status,
    userName: nonEmpty(v.user_name),
    workspace: { id: nonEmpty(workspace.id), name: nonEmpty(workspace.name), urlKey: nonEmpty(workspace.url_key) },
  };
}

export function createHttpLinear(): LinearPort {
  return {
    connection: async () => {
      const body = await guard(() => getLinearConnection());
      return checked(() => toLinearConnection(body));
    },
    authorize: async (returnTo) => {
      const body = await guard(() => authorizeLinear(returnTo));
      return checked(() => {
        expectShape(isRecord(body));
        return linearUrl(body.url);
      });
    },
  };
}
