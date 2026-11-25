// What the server's read of one project means for a page (pure: no Next.js, no env), so it is tested without a server.

export type ProjectRead = { name: string } | "not_found" | null;
const NOT_FOUND: ProjectRead = "not_found";
const UNREADABLE: ProjectRead = null;

/**
 * What the project read answered: 404 (an unknown id) and 422 (an id that is not a UUID) both mean "not this caller's project"; any other
 * failure is `null`, so the page still renders and shows its own error.
 */
export function projectNameOf(answer: { status: number; body: string }): ProjectRead {
  if (answer.status === 404 || answer.status === 422) return NOT_FOUND;
  if (answer.status !== 200) return UNREADABLE;
  let read: ProjectRead = UNREADABLE;
  try {
    const body: unknown = JSON.parse(answer.body);
    const name = typeof body === "object" && body !== null && "name" in body ? body.name : undefined;
    if (typeof name === "string") read = { name };
  } catch {
    read = UNREADABLE;
  }
  return read;
}
