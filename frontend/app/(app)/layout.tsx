// FE-BP-01, FE-BP-03, FE-DATA-01, SPL-DEMO-01 (TECH-SPECS §1.2, §5.4): the production tree (P17). Server: a session is required (the
// proxy is only an optimistic gate), then the production data provider (http for /me, projects, create, Mission Control and sources;
// the `unavailable` adapter for the rest) around the AppShell. No fixture is loaded.
import type { ReactNode } from "react";
import { DataProvider } from "@/lib/data/provider";
import { requireUser } from "@/lib/page-session";
import { ProductionShell } from "./production-shell";
import "./production.css";

export default async function ProductionLayout({ children }: Readonly<{ children: ReactNode }>) {
  await requireUser(); // FE-BP-03.AC6: no session -> /login
  // FE-DS-07 (P17 s03): the theme root is <html data-theme> (root layout); this wrapper only carries the design-system root
  // classes.
  return (
    <div className="gdg-root body" data-testid="production-root">
      <DataProvider>
        <ProductionShell>{children}</ProductionShell>
      </DataProvider>
    </div>
  );
}
