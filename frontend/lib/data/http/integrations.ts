// docs/mission-control/integrations-api.md: the production `IntegrationsPort` over `/projects/{pid}/integrations`. Every body is
// shape-checked (like the Jira port): any other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code.
import { deleteProjectIntegration, listProjectIntegrations, putProjectIntegration } from "@/lib/api";
import { INTEGRATION_APPS, type Integration, type IntegrationHealth, type IntegrationsPort } from "@/lib/domain/integrations";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const HEALTH: readonly IntegrationHealth[] = ["active", "reconnect_required", "none"];

function pairOrNull(v: unknown, idKey: string, nameKey: string): { id: string; name: string } | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v) && isString(v[idKey]) && isString(v[nameKey]));
  return { id: v[idKey], name: v[nameKey] };
}

const whenOrNull = (v: unknown): string | null => {
  if (v === null || v === undefined) return null;
  expectShape(isString(v) && !Number.isNaN(Date.parse(v)));
  return v;
};

export function toIntegration(v: unknown): Integration {
  expectShape(isRecord(v) && typeof v.available === "boolean");
  const app = INTEGRATION_APPS.find((a) => a === v.app);
  const health = HEALTH.find((h) => h === v.health);
  expectShape(app !== undefined && health !== undefined);
  const by = pairOrNull(v.connected_by, "user_id", "name");
  const workspace = pairOrNull(v.workspace, "id", "name");
  return {
    app,
    available: v.available,
    connectedBy: by === null ? null : { userId: by.id, name: by.name },
    workspace,
    connectedAt: whenOrNull(v.connected_at),
    health,
  };
}

/** `{integrations: [three]}` in the API's order: another count or order is not the contract. */
export function toIntegrations(v: unknown): Integration[] {
  expectShape(isRecord(v) && Array.isArray(v.integrations) && v.integrations.length === INTEGRATION_APPS.length);
  const list = v.integrations.map(toIntegration);
  expectShape(list.every((row, i) => row.app === INTEGRATION_APPS[i]));
  return list;
}

export function createHttpIntegrations(): IntegrationsPort {
  return {
    list: async (projectId) => {
      const body = await guard(() => listProjectIntegrations(projectId));
      return checked(() => toIntegrations(body));
    },
    connect: async (projectId, app) => {
      const body = await guard(() => putProjectIntegration(projectId, app));
      return checked(() => toIntegration(body));
    },
    disconnect: async (projectId, app) => {
      await guard(() => deleteProjectIntegration(projectId, app));
    },
  };
}
