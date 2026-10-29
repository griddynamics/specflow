// FR-SRC-0002 (mockup "Cards B2"): the source logo in a 64 px ring. Track in the hairline colour; Processing = a yellow arc as long as the
// progress (none while the source lists its files); Done and Done with problems = a full green ring; Failed = a full red ring. The colours
// come from tokens through mc-sources.css (the circles carry classes, not colours). The state is told to assistive technology by the card
// (cardView().srText), so the SVG is decorative.
import type { ReactNode } from "react";
import type { SourceState } from "@/lib/domain/sources";

const R = 30;
const LENGTH = 2 * Math.PI * R;

export function SourceRing({
  state,
  progress,
  listing,
  children,
}: Readonly<{
  state: SourceState;
  progress: number;
  listing: boolean;
  children?: ReactNode;
}>) {
  const processing = state === "processing";
  let share = 1; // Done, Done with problems and Failed: a full ring
  if (processing) share = listing ? 0 : Math.max(0, Math.min(1, progress)); // nothing while it lists, else the progress clamped to 0..1
  return (
    <span className="mcs-ring" data-testid="source-ring" data-state={state} data-progress={Math.round(share * 100)}>
      <svg className="mcs-ring__svg" width="64" height="64" viewBox="0 0 64 64" aria-hidden focusable="false">
        <circle className="mcs-ring__track" cx="32" cy="32" r={R} fill="none" strokeWidth="3" data-testid="source-ring-track" />
        {share > 0 ? (
          <circle
            className="mcs-ring__arc"
            cx="32"
            cy="32"
            r={R}
            fill="none"
            strokeWidth="3"
            strokeLinecap="round"
            strokeDasharray={LENGTH}
            strokeDashoffset={LENGTH * (1 - share)}
          />
        ) : null}
      </svg>
      <span className="mcs-ring__logo">{children}</span>
    </span>
  );
}
