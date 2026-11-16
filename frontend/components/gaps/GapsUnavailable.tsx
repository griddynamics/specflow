// FR-GWEB-04: what the Gaps screen shows when the server answers `503 gap_unavailable` (gap runs are not configured).
import { EmptyState } from "@/components/ui/EmptyState";

export function GapsUnavailable() {
  return (
    <div data-testid="gaps-unavailable">
      <EmptyState title="Gaps are not available" icon="warning-circle">
        Gap checks are not set up on this server. Ask your administrator to turn them on.
      </EmptyState>
    </div>
  );
}
