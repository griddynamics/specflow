// AC1, D16 (Mission Control V2): the route group of the new screens: `/`, `/projects/<id>` (the canvas) and `/projects/<id>/integrations`.
// Server: a session is required (the proxy is only an optimistic gate), then the production data provider around the V2 shell. The V1
// screens stay at their own URLs under app/(app) until the clean-up removes them.
import type { ReactNode } from "react";
import { V2Shell } from "@/components/v2/shell/V2Shell";
import { DataProvider } from "@/lib/data/provider";
import { requireUser } from "@/lib/page-session";

export default async function V2Layout({ children }: Readonly<{ children: ReactNode }>) {
  await requireUser(); // no session -> /login
  return (
    <DataProvider>
      <V2Shell>{children}</V2Shell>
    </DataProvider>
  );
}
