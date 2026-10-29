// FE-PLAN-10.AC1/AC2, FE-DEC-02.AC5, FE-DS-08.AC2, D20 (MOCKS §10.2, §15.8 noPlan*): the empty state of the shared gate; icon, title and body differ per screen.
// Navigation is the caller's (no router here: SSR-testable). The gate sits on the `.gdg-scene` backdrop (`.gate-scene`, ready-gate.css, imported by the two screens: this file stays CSS-free for node tests).
import type { ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import type { IconName } from "@/components/ui/Icon";

export function NotReadyState({
  testId,
  icon,
  title,
  children,
  onOpenMissionControl,
}: Readonly<{
  testId: string;
  icon: IconName;
  title: string;
  children: ReactNode;
  onOpenMissionControl: () => void;
}>) {
  return (
    <div className="gate-scene gdg-scene" data-testid={testId}>
      <EmptyState title={title} icon={icon} actions={<Button onClick={onOpenMissionControl}>Open Mission Control</Button>}>
        {children}
      </EmptyState>
    </div>
  );
}
