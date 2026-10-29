"use client";
// FR-SRC-0001..0005, NFR-USE-0003 (mockup "Cards B2"): one card for every source kind: the logo in a ring, the name, a second line and one
// count line. No buttons, menus or state tags. The whole card opens the details: the name is a button inside the heading (a heading
// inside a button is invalid HTML) stretched over the card by CSS, so a click, Enter and Space all work and the card is one tab stop.
// The state is told to assistive technology as the button's description ("Done, 2 files need attention").
import { useId } from "react";
import { logoSrc } from "@/components/connections/logos";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import type { Source } from "@/lib/domain/sources";
import { cardView, ringProgress } from "./source-logic";
import { SourceRing } from "./SourceRing";
import { truthy } from "@/components/ui/utils";

const LOGO = { gdrive: "googledrive", github: "github" } as const;

export function SourceLogo({ kind }: Readonly<{ kind: Source["kind"] }>) {
  if (kind === "upload") return <Icon name="upload-simple" size={24} />;
  const src = logoSrc(LOGO[kind]);
  return truthy(src) ? <img src={src} alt="" width={28} height={28} /> : null;
}

export function SourceCard({ source, onOpen }: Readonly<{ source: Source; onOpen(id: string): void }>) {
  const v = cardView(source);
  const sr = useId();
  return (
    <li className="mcs-card-item">
      <GlassSurface
        variant="strong"
        className="mcs-card"
        data-testid="source-card"
        data-id={source.id}
        data-kind={source.kind}
        data-state={v.state}
      >
        <SourceRing state={v.state} progress={ringProgress(source)} listing={source.listing}>
          <SourceLogo kind={source.kind} />
        </SourceRing>
        <div className="mcs-card__body">
          <h3 className="body mcs-card__name">
            <button
              type="button"
              className="mcs-card__open"
              data-testid="source-open"
              title={source.name}
              aria-describedby={sr}
              onClick={() => {
                onOpen(source.id);
              }}
            >
              {source.name}
            </button>
          </h3>
          <p className="caption mcs-card__line2">{v.line2}</p>
          <p className={`body-sm mcs-card__count${v.warn ? " mcs-card__count--warn" : ""}`} data-testid="source-counts">
            {v.warn ? (
              <span className="mcs-card__warn" data-testid="source-warning" role="img" aria-label="Some files were not processed">
                <Icon name="warning-circle" size={16} />
              </span>
            ) : null}
            <span className="mcs-card__count-text" data-testid="source-count-text">
              {v.countLine}
            </span>
          </p>
        </div>
        <span id={sr} className="mc-sr" data-testid="source-state-text">
          {v.srText}
        </span>
      </GlassSurface>
    </li>
  );
}
