// docs/notion/oauth.md "Routes": the production `NotionConnectionPort` over `/integrations/notion`. Every body is shape-checked: any other shape
// is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code.
import { authorizeNotion, disconnectNotion, getNotionConnection } from "@/lib/api";
import type { NotionConnection, NotionConnectionPort } from "@/lib/domain/notion-connection";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const text = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return v ?? null;
};

const when = (v: unknown): string | null => {
  const t = text(v);
  expectShape(t === null || !Number.isNaN(Date.parse(t)));
  return t;
};

export function toNotionConnection(v: unknown): NotionConnection {
  expectShape(isRecord(v) && typeof v.enabled === "boolean" && typeof v.connected === "boolean");
  const status = v.status ?? null;
  expectShape(status === null || status === "active" || status === "expired");
  expectShape(v.connected === (status !== null));
  return {
    enabled: v.enabled,
    connected: v.connected,
    status,
    workspaceName: text(v.workspace_name),
    connectedAt: when(v.connected_at),
    lastUsedAt: when(v.last_used_at),
  };
}

/** `POST /integrations/notion/authorize` -> `{url}`: Notion's own https page, nothing else. */
export function toNotionAuthorize(v: unknown): { url: string } {
  expectShape(isRecord(v) && isString(v.url) && URL.canParse(v.url) && new URL(v.url).protocol === "https:");
  return { url: v.url };
}

export function createHttpNotionConnection(): NotionConnectionPort {
  return {
    connection: async () => {
      const body = await guard(() => getNotionConnection());
      return checked(() => toNotionConnection(body));
    },
    authorize: async (returnTo) => {
      const body = await guard(() => authorizeNotion(returnTo));
      return checked(() => toNotionAuthorize(body));
    },
    disconnect: async () => {
      await guard(() => disconnectNotion());
    },
  };
}
