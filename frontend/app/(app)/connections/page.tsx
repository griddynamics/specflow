// FE-BP-01, FE-BP-03 (TECH-SPECS §1.2): production tree (P17). Thin binding, the (app) layout picks the production adapter.
import { Suspense } from "react";
import { ConnectionsScreen } from "@/components/connections/ConnectionsScreen";

export default function Page() {
  return (
    <Suspense>
      <ConnectionsScreen />
    </Suspense>
  );
}
