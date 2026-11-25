// plans/LINEAR/LINEAR-PLAN.md, stream S4: the production `LinearSourcePort` over `/projects/{pid}/sources/linear` (check, add). Every body is
// shape-checked: any other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code.
import { addLinearSourceLink, checkLinearSourceLink } from "@/lib/api";
import type { LinearSourceCheck, LinearSourceItem, LinearSourcePort } from "@/lib/domain/linear-source";
import { expectShape, guard, isRecord, isString } from "./errors";

const toItem = (item: unknown): LinearSourceItem => {
  expectShape(
    isRecord(item) &&
      isString(item.identifier) &&
      item.identifier !== "" &&
      isString(item.title) &&
      isString(item.state) &&
      (item.parent === null || isString(item.parent)),
  );
  return { identifier: item.identifier, title: item.title, stateName: item.state, parent: item.parent };
};

const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

/** `POST .../sources/linear/check` -> `LinearSourceCheck`; any other shape is `invalid_response`. */
export function toLinearSourceCheck(body: unknown): LinearSourceCheck {
  expectShape(
    isRecord(body) &&
      (body.kind === "project" || body.kind === "issue") &&
      isString(body.name) &&
      isCount(body.issues) &&
      isCount(body.documents) &&
      typeof body.capped === "boolean" &&
      Array.isArray(body.items),
  );
  return {
    kind: body.kind,
    name: body.name,
    issues: body.issues,
    items: (body.items as unknown[]).map(toItem),
    capped: body.capped,
    documents: body.documents,
  };
}

export function createHttpLinearSource(): LinearSourcePort {
  return {
    async check(projectId, linkOrIdentifier) {
      return toLinearSourceCheck(await guard(() => checkLinearSourceLink(projectId, linkOrIdentifier)));
    },
    async add(projectId, linkOrIdentifier, exclude) {
      await guard(() => addLinearSourceLink(projectId, linkOrIdentifier, exclude));
    },
  };
}
