// docs/box/oauth.md "Routes": the production `BoxPort` over the caller's Box connection. Every body is shape-checked (like the GitHub port): any other
// shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code. The URL the browser follows must be `https:` on box.com.
import { authorizeBox, getBoxConnection } from "@/lib/api";
import type { BoxConnection, BoxPort } from "@/lib/domain/box";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATUSES: readonly NonNullable<BoxConnection["status"]>[] = ["active", "expired"];
const BOX_HOST = "box.com";

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || isString(v));
  return v;
};
const whenOrNull = (v: unknown): string | null => {
  const text = textOrNull(v);
  expectShape(text === null || !Number.isNaN(Date.parse(text)));
  return text;
};

/** An `https:` URL on box.com (or one of its subdomains: `account.box.com`); anything else is not the contract. */
function boxUrl(v: unknown): string {
  expectShape(isString(v) && URL.canParse(v));
  const url = new URL(v);
  expectShape(url.protocol === "https:" && (url.hostname === BOX_HOST || url.hostname.endsWith(`.${BOX_HOST}`)));
  return url.href;
}

export function toBoxConnection(v: unknown): BoxConnection {
  expectShape(isRecord(v) && typeof v.enabled === "boolean" && typeof v.connected === "boolean");
  const status = v.status === null ? null : STATUSES.find((s) => s === v.status);
  expectShape(status !== undefined && (status !== null) === v.connected);
  return {
    enabled: v.enabled,
    connected: v.connected,
    status,
    name: textOrNull(v.name),
    enterpriseName: textOrNull(v.enterprise_name),
    connectedAt: whenOrNull(v.connected_at),
    lastUsedAt: whenOrNull(v.last_used_at),
  };
}

export function createHttpBox(): BoxPort {
  return {
    connection: async () => {
      const body = await guard(() => getBoxConnection());
      return checked(() => toBoxConnection(body));
    },
    authorize: async (returnTo) => {
      const body = await guard(() => authorizeBox(returnTo));
      return checked(() => {
        expectShape(isRecord(body));
        return boxUrl(body.url);
      });
    },
  };
}
