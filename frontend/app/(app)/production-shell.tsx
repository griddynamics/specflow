"use client";
// FE-BP-03.AC3, AC4 (TECH-SPECS §1.7, O12): the production wrapper around the shared AppShell. It adds what only production has: the
// avatar menu item "Sign out" (the production provider has no reset) and the sign-out routine that a 401 from the BFF starts
// (`components/shell/use-sign-out.ts`, shared with the V2 shell).
import { useMemo, type ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import { useSignOut } from "@/components/shell/use-sign-out";
import type { MenuItem } from "@/components/ui/Menu";
import { Spinner } from "@/components/ui/ProgressBar";

export function ProductionShell({ children }: Readonly<{ children: ReactNode }>) {
  const { signingOut, endSession } = useSignOut();

  const accountItems = useMemo<MenuItem[]>(
    () => [{ label: "Sign out", icon: "sign-out", onSelect: () => void endSession() }],
    [endSession],
  );

  if (signingOut) {
    return (
      <div className="signing-out" data-testid="signing-out">
        <Spinner size={24} label="Signing out" />
      </div>
    );
  }
  return <AppShell accountItems={accountItems}>{children}</AppShell>;
}
