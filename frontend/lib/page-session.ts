import { redirect } from "next/navigation";
import { auth } from "@/auth";

/** Page-level guard: the session user, or a redirect to /login. */
export async function requireUser() {
  const session = await auth();
  if (!session?.user) redirect("/login");
  return { email: session.user.email ?? session.user.name ?? "" };
}
