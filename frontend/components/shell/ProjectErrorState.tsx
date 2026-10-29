// FE-BP-01, FE-BP-03 (TECH-SPECS §1.2): the fallback of the `projects/[projectId]` error boundary. A project screen that throws while rendering (for example for a project id that names nothing) shows this state
// inside the shell instead of taking the whole page down. It never shows the error text (production errors carry no details).
// Navigation is the caller's (no router here: SSR-testable).
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";

export function ProjectErrorState({ onRetry }: Readonly<{ onRetry: () => void }>) {
  return (
    <div data-testid="project-error">
      <EmptyState title="This project could not be shown" icon="warning-circle" actions={<Button onClick={onRetry}>Try again</Button>}>
        Check your connection and try again, or pick another project.
      </EmptyState>
    </div>
  );
}
