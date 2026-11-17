// docs/projects/ui.md#ports: the members of a project, its pending invites and the email suggestions. Screens reach them only
// through `MembersPort`; the http adapter (`lib/data/http/members.ts`) is the one implementation.

export type MemberRole = "owner" | "collaborator";

export interface Member {
  userId: string;
  name: string | null; // null when the display name is an address (api.md#shapes): the screen shows the email instead
  email: string;
  role: MemberRole;
  joinedAt: string;
  isYou: boolean;
}

export interface Invite {
  id: string;
  email: string; // normalised by the API
  invitedAt: string;
}

/** `GET /projects/{pid}/members`: `role` is the caller's. Order: the owner, then members by join date; invites by creation. */
export interface MembersState {
  role: MemberRole;
  limit: number;
  members: Member[];
  invites: Invite[];
}

export type AddedPerson = { kind: "member"; member: Member } | { kind: "invite"; invite: Invite };

/** What removing a member or leaving did to the person's story claims (api.md#remove-leave-and-claims). */
export interface ClaimsOutcome {
  claimsReleased: number;
  claimsReleaseFailed: boolean;
}

/** A suggested person: name and email only, never an id (api.md#suggestions). */
export interface Suggestion {
  name: string;
  email: string;
}

export interface MembersPort {
  list(projectId: string): Promise<MembersState>;
  add(projectId: string, email: string): Promise<AddedPerson>; // owner only
  remove(projectId: string, userId: string): Promise<ClaimsOutcome>; // owner only
  cancelInvite(projectId: string, inviteId: string): Promise<void>; // owner only
  leave(projectId: string): Promise<ClaimsOutcome>; // collaborator only
  suggest(query: string, projectId?: string): Promise<Suggestion[]>; // without `projectId`: the Create Project flow
}

/** Suggestions start from this many typed characters (api.md#suggestions). */
export const SUGGEST_MIN_CHARS = 3;
