// docs/projects/api.md#routes, docs/projects/ui.md#ports: the production `MembersPort` over the member, invite, leave and suggestion
// routes. Every body is shape-checked (like `toProject`): any other shape is `invalid_response`. Failures are `DataError`s whose
// `detail.apiCode` is the API code. Emails travel in bodies only, never in a path or query (NFR-PRJ-02).
import { addMember, cancelInvite, leaveProject, listMembers, removeMember, suggestUsers } from "@/lib/api";
import type { AddedPerson, ClaimsOutcome, Invite, Member, MemberRole, MembersPort, MembersState, Suggestion } from "@/lib/domain/members";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const ROLES: readonly MemberRole[] = ["admin", "collaborator"];

const roleOf = (v: unknown): MemberRole => {
  const found = ROLES.find((r) => r === v);
  expectShape(found !== undefined);
  return found;
};

export function toMember(v: unknown): Member {
  expectShape(isRecord(v) && isString(v.user_id) && v.user_id !== "" && isString(v.email) && isString(v.joined_at));
  expectShape(v.name === null || isString(v.name));
  expectShape(typeof v.is_you === "boolean");
  return { userId: v.user_id, name: v.name, email: v.email, role: roleOf(v.role), joinedAt: v.joined_at, isYou: v.is_you };
}

export function toInvite(v: unknown): Invite {
  expectShape(isRecord(v) && isString(v.id) && v.id !== "" && isString(v.email) && isString(v.invited_at));
  return { id: v.id, email: v.email, invitedAt: v.invited_at };
}

export function toMembersState(v: unknown): MembersState {
  expectShape(isRecord(v) && typeof v.limit === "number" && Number.isInteger(v.limit) && v.limit > 0);
  expectShape(Array.isArray(v.members) && Array.isArray(v.invites));
  return { role: roleOf(v.role), limit: v.limit, members: v.members.map(toMember), invites: v.invites.map(toInvite) };
}

export function toAdded(v: unknown): AddedPerson {
  expectShape(isRecord(v));
  if (v.kind === "member") return { kind: "member", member: toMember(v.member) };
  expectShape(v.kind === "invite");
  return { kind: "invite", invite: toInvite(v.invite) };
}

export function toClaimsOutcome(v: unknown): ClaimsOutcome {
  expectShape(isRecord(v) && typeof v.claims_released === "number" && Number.isInteger(v.claims_released) && v.claims_released >= 0);
  expectShape(typeof v.claims_release_failed === "boolean");
  return { claimsReleased: v.claims_released, claimsReleaseFailed: v.claims_release_failed };
}

export function toSuggestions(v: unknown): Suggestion[] {
  expectShape(isRecord(v) && Array.isArray(v.items));
  return v.items.map((item: unknown) => {
    expectShape(isRecord(item) && isString(item.name) && isString(item.email));
    return { name: item.name, email: item.email };
  });
}

export function createHttpMembers(): MembersPort {
  return {
    list: async (projectId) => {
      const body = await guard(() => listMembers(projectId));
      return checked(() => toMembersState(body));
    },
    add: async (projectId, email) => {
      const body = await guard(() => addMember(projectId, email));
      return checked(() => toAdded(body));
    },
    remove: async (projectId, userId) => {
      const body = await guard(() => removeMember(projectId, userId));
      return checked(() => toClaimsOutcome(body));
    },
    async cancelInvite(projectId, inviteId) {
      await guard(() => cancelInvite(projectId, inviteId));
    },
    leave: async (projectId) => {
      const body = await guard(() => leaveProject(projectId));
      return checked(() => toClaimsOutcome(body));
    },
    suggest: async (query, projectId) => {
      const body = await guard(() => suggestUsers(query, projectId));
      return checked(() => toSuggestions(body));
    },
  };
}
