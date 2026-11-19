// FR-GWEB-04 (api.md "User interface": the Gaps screen replaced the Decisions screen): the old Decisions URL redirects (307) to the
// project's Gaps screen with the same query string. The target route checks the session and the project itself.
import { redirect } from "next/navigation";
import { routesFor } from "@/lib/routes";

type Query = Record<string, string | string[] | undefined>;

export default async function OldDecisionsPage({
  params,
  searchParams,
}: Readonly<{ params: Promise<{ projectId: string }>; searchParams: Promise<Query> }>) {
  const { projectId } = await params;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) {
    for (const v of [value ?? []].flat()) query.append(key, v);
  }
  const qs = query.toString();
  const target = routesFor("production").gaps(projectId);
  redirect(qs === "" ? target : `${target}?${qs}`);
}
