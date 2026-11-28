// docs/dropbox/oauth.md "Routes": the production `DropboxPort` over the caller's Dropbox connection. Every body is shape-checked (like the GitHub port): any other
// shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code. The URL the browser follows must be `https:` on dropbox.com (www.dropbox.com).
import { authorizeDropbox, getDropboxConnection } from "@/lib/api";
import type { DropboxConnection, DropboxPort } from "@/lib/domain/dropbox";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATUSES: readonly NonNullable<DropboxConnection["status"]>[] = ["active", "expired"];
const DROPBOX_HOST = "dropbox.com";

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || isString(v));
  return v;
};
const whenOrNull = (v: unknown): string | null => {
  const text = textOrNull(v);
  expectShape(text === null || !Number.isNaN(Date.parse(text)));
  return text;
};

/** An `https:` URL on dropbox.com (or one of its subdomains: `www.dropbox.com`); anything else is not the contract. */
function dropboxUrl(v: unknown): string {
  expectShape(isString(v) && URL.canParse(v));
  const url = new URL(v);
  expectShape(url.protocol === "https:" && (url.hostname === DROPBOX_HOST || url.hostname.endsWith(`.${DROPBOX_HOST}`)));
  return url.href;
}

export function toDropboxConnection(v: unknown): DropboxConnection {
  expectShape(isRecord(v) && typeof v.enabled === "boolean" && typeof v.connected === "boolean");
  const status = v.status === null ? null : STATUSES.find((s) => s === v.status);
  expectShape(status !== undefined && (status !== null) === v.connected);
  return {
    enabled: v.enabled,
    connected: v.connected,
    status,
    name: textOrNull(v.name),
    teamName: textOrNull(v.team_name),
    connectedAt: whenOrNull(v.connected_at),
    lastUsedAt: whenOrNull(v.last_used_at),
  };
}

export function createHttpDropbox(): DropboxPort {
  return {
    connection: async () => {
      const body = await guard(() => getDropboxConnection());
      return checked(() => toDropboxConnection(body));
    },
    authorize: async (returnTo) => {
      const body = await guard(() => authorizeDropbox(returnTo));
      return checked(() => {
        expectShape(isRecord(body));
        return dropboxUrl(body.url);
      });
    },
  };
}
