"use client";
// Mission Control sources (DESIGN D1, D4, D6; owner redesign 2026-10-15; sources-ux FR-SRC-0001, 0006): the source list, the only content of
// Mission Control. One card per source, in the order the sources were added, the same layout for Google Drive, GitHub and Local files
// batches (SourceCard). No visible heading (owner): "Add source" sits on the header row (HeaderActions); the section keeps a hidden heading
// for screen readers.
import { EmptyState } from "@/components/ui/EmptyState";
import type { ProjectSourcesStatus } from "@/lib/domain/sources";
import { SourceCard } from "./SourceCard";

export function SourcesSection({
  status,
  onOpen,
  github = true,
}: Readonly<{
  status: ProjectSourcesStatus;
  onOpen(id: string): void;
  github?: boolean;
}>) {
  return (
    <section className="mcs-section" aria-labelledby="mcs-sources-title" data-testid="mcs-sources">
      <h2 className="mc-sr" id="mcs-sources-title">
        Sources
      </h2>
      {status.sources.length === 0 ? (
        <div className="mc-scene gdg-scene mcs-empty" data-testid="mcs-empty">
          <EmptyState icon="plug" title="Connect your project's sources">
            {github
              ? "Use Add source to connect a GitHub repository, a Google Drive folder or local files. Specflow keeps them in sync; you build the knowledge when you are ready."
              : "Use Add source to connect a Google Drive folder or upload local files. Specflow collects them; you build the knowledge when you are ready."}
          </EmptyState>
        </div>
      ) : (
        <ul className="mcs-cards" aria-label="Sources">
          {status.sources.map((s) => (
            <SourceCard key={s.id} source={s} onOpen={onOpen} />
          ))}
        </ul>
      )}
    </section>
  );
}
