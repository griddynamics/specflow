// FE-PPL-01.AC3, FE-BP-02.AC8 (TECH-SPECS §2.3, §3.3): the active user from `GET /me`.
// `MeOut { id, actor_id, email, display_name }` -> `Person { id, name: display_name, email, kind: "person" }`.
// Read once per adapter (FE-BP-02.AC8 "once"); a failed read is not kept, so the next call asks again.
import type { PeoplePort, Person } from "@/lib/domain/people";
import { getJson } from "./client";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

export interface MeOut {
  id: string;
  actor_id?: string;
  email?: string | null;
  display_name?: string | null;
}

const isMeOut = (raw: unknown): raw is MeOut => isRecord(raw) && isString(raw.id) && raw.id !== "";

/** Pure: the API user -> a Person. A missing display name falls back to the email, then to the id. */
export function toPerson(raw: unknown): Person {
  const me = checked(() => {
    expectShape(isMeOut(raw));
    return raw;
  });
  const email = isString(me.email) ? me.email : undefined;
  const display = isString(me.display_name) ? me.display_name : undefined;
  const displayName = display?.trim();
  const emailName = email?.trim();
  let name = me.id;
  if (displayName !== undefined && displayName !== "") name = displayName;
  else if (emailName !== undefined && emailName !== "") name = emailName;
  return { id: me.id, name, ...(email !== undefined && email !== "" ? { email } : {}), kind: "person" };
}

export function createHttpPeople(): Pick<PeoplePort, "me"> {
  let cached: Promise<Person> | null = null;
  return {
    me() {
      if (!cached) {
        const p = guard(() => getJson<unknown>("/me")).then(toPerson); // `{}` / null -> invalid_response (QA B1)
        cached = p;
        p.catch(() => {
          if (cached === p) cached = null;
        });
      }
      return cached.then((p) => ({ ...p }));
    },
  };
}
