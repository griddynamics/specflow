// docs/slack/gap-answering.md "The channel flow": the production `SlackPort` over `/projects/{pid}/slack`. Every body is shape-checked
// (like `lib/data/http/jira.ts`): any other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code.
import { deleteSlack, getSlack } from "@/lib/api";
import type { SlackChannel, SlackPort, SlackSettings } from "@/lib/domain/slack";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

function toChannel(v: unknown): SlackChannel {
  expectShape(isRecord(v) && isString(v.id) && isString(v.name) && typeof v.private === "boolean");
  return { id: v.id, name: v.name, private: v.private };
}

export function toSlackSettings(v: unknown): SlackSettings {
  expectShape(isRecord(v) && typeof v.available === "boolean" && typeof v.editable === "boolean");
  return {
    available: v.available,
    channel: v.channel === null || v.channel === undefined ? null : toChannel(v.channel),
    editable: v.editable,
  };
}

export function createHttpSlack(): SlackPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getSlack(projectId));
      return checked(() => toSlackSettings(body));
    },
    unbind: async (projectId) => {
      await guard(() => deleteSlack(projectId));
    },
  };
}
