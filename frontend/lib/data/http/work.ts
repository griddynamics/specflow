// MCP-TOOLING session 10, FE-DATA-01: the production `WorkPort` over the findings and claims API (`/projects/{pid}/findings*`,
// `/projects/{pid}/plan/claims*`). Bodies are parsed strictly: any other shape is `invalid_response`. A project without a plan has no
// claims (`404 no_plan` reads as an empty board). Holders are names only; no id, email or token is read or kept.
import { dismissFinding, getClaims, getFindings, releaseClaim, resolveFinding } from "@/lib/api";
import { DataError } from "@/lib/domain/errors";
import type { Claim, ClaimKind, Finding, FindingKind, FindingRef, FindingStatus, Person, RefType, WorkPort } from "@/lib/domain/work";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const KINDS: readonly FindingKind[] = ["gap", "conflict", "question", "note"];
const STATUSES: readonly FindingStatus[] = ["open", "resolved", "dismissed"];
const REF_TYPES: readonly RefType[] = ["pr", "commit", "file", "url"];
const CLAIM_KINDS: readonly ClaimKind[] = ["story", "task"];

const oneOf = <T extends string>(values: readonly T[], v: unknown): T => {
  const found = values.find((x) => x === v);
  expectShape(found !== undefined);
  return found;
};
const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return isString(v) ? v : null;
};
const count = (v: unknown): number => {
  expectShape(typeof v === "number" && Number.isInteger(v) && v >= 0);
  return v;
};

function toPerson(v: unknown): Person {
  expectShape(isRecord(v) && typeof v.you === "boolean");
  return { name: textOrNull(v.name), you: v.you, agentLabel: textOrNull(v.agent_label) };
}

export function toFinding(v: unknown): Finding {
  expectShape(isRecord(v) && isString(v.finding_id) && isString(v.text) && isString(v.created_at) && typeof v.blocking === "boolean");
  expectShape(Array.isArray(v.about) && v.about.every(isString) && Array.isArray(v.refers_to));
  const refs: FindingRef[] = v.refers_to.map((r: unknown) => {
    expectShape(isRecord(r) && isString(r.value));
    return { type: oneOf(REF_TYPES, r.type), value: r.value };
  });
  return {
    findingId: v.finding_id,
    kind: oneOf(KINDS, v.kind),
    text: v.text,
    status: oneOf(STATUSES, v.status),
    blocking: v.blocking,
    about: v.about,
    refersTo: refs,
    storyId: textOrNull(v.story_id),
    storyTitle: textOrNull(v.story_title),
    author: toPerson(v.author),
    createdAt: v.created_at,
    resolvedBy: v.resolved_by === null || v.resolved_by === undefined ? null : toPerson(v.resolved_by),
    resolvedAt: textOrNull(v.resolved_at),
    resolutionNote: textOrNull(v.resolution_note),
  };
}

export function toClaim(v: unknown): Claim {
  expectShape(isRecord(v) && isString(v.story_id) && isString(v.title) && isString(v.claimed_at));
  return {
    kind: oneOf(CLAIM_KINDS, v.kind),
    storyId: v.story_id,
    title: v.title,
    taskId: textOrNull(v.task_id),
    holder: toPerson(v.holder),
    claimedAt: v.claimed_at,
    branch: textOrNull(v.branch),
    tasks: count(v.tasks),
  };
}

export function createHttpWork(): WorkPort {
  return {
    async findings(projectId, opts) {
      const body = await guard(() => getFindings(projectId, opts?.status ?? "open", opts?.node));
      return checked(() => {
        expectShape(isRecord(body) && Array.isArray(body.findings));
        return { findings: body.findings.map(toFinding), more: isString(body.next) };
      });
    },
    async close(projectId, findingId, action, note) {
      const send = action === "resolve" ? resolveFinding : dismissFinding;
      const body = await guard(() => send(projectId, findingId, note));
      return checked(() => {
        expectShape(isRecord(body));
        return { finding: toFinding(body.finding), unblocked: count(body.unblocked) };
      });
    },
    async claims(projectId) {
      let body: unknown;
      try {
        body = await guard(() => getClaims(projectId));
      } catch (e) {
        if (e instanceof DataError && e.detail?.apiCode === "no_plan") return [];
        throw e;
      }
      return checked(() => {
        expectShape(isRecord(body) && Array.isArray(body.claims));
        return body.claims.map(toClaim);
      });
    },
    async release(projectId, storyId) {
      const body = await guard(() => releaseClaim(projectId, storyId));
      return checked(() => {
        expectShape(isRecord(body));
        return { released: count(body.released) };
      });
    },
  };
}
