"use client";
// MCP-M10 (U4): SpecFlow 101. Five step tiles (aria-pressed, `glass-tint` when selected, step 1 at first) and the detail card. A tile is a
// large icon in a glass medallion with the number badge, and the name; a connector joins the tiles into a flow (guides.css). The detail card
// holds all the text. One accent per view: the selected tile's yellow tint on steps 1 to 4; on step 5 the primary "Connect your coding
// agent" link is the yellow, so that tile gets a neutral wash. The selected badge is a ring.
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { ButtonLink } from "@/components/ui/ButtonLink";
import { Card } from "@/components/ui/Card";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import { FLOW_101, FLOW_STEPS } from "@/lib/guides/content";
import { cx } from "@/components/ui/utils";

/** `connectHref` is the MCP server guide's route, passed by the server page. */
export function Flow101({ connectHref }: Readonly<{ connectHref: string }>) {
  const [index, setIndex] = useState(0);
  const last = FLOW_STEPS.length - 1;
  const step = FLOW_STEPS[index];
  return (
    <div className="guides-flow" data-testid="guide-flow">
      <ol className="guides-flow__list" aria-label={FLOW_101.listLabel}>
        {FLOW_STEPS.map((s, i) => {
          const selected = i === index;
          return (
            <li key={s.name}>
              <button
                type="button"
                className={cx(
                  "guides-flow__step",
                  selected && "guides-flow__step--selected",
                  selected && i === last && "guides-flow__step--last",
                )}
                aria-pressed={selected}
                onClick={() => {
                  setIndex(i);
                }}
              >
                <span className="guides-flow__medal">
                  <Icon name={s.icon} size={44} />
                  <span className={cx("guides-flow__num body-sm", selected && "guides-flow__num--ring")}>{i + 1}</span>
                </span>
                <span className="headline">{s.name}</span>
              </button>
              {i < last ? (
                <span className="guides-flow__link" aria-hidden>
                  <Icon name="caret-right" size={16} />
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
      <Card variant="strong" eyebrow={`Step ${String(index + 1)} of ${String(FLOW_STEPS.length)}`} title={step.name}>
        <div className="guides-flow__detail" data-testid="guide-flow-detail">
          <p className="body-lg">{step.detail}</p>
          <div className="guides-flow__boxes">
            <GlassSurface variant="regular" className="guides-flow__box">
              <span className="eyebrow">{FLOW_101.whereLabel}</span>
              <span className="body">{step.where}</span>
            </GlassSurface>
            <GlassSurface variant="regular" className="guides-flow__box">
              <span className="eyebrow">{FLOW_101.getLabel}</span>
              <span className="body">{step.get}</span>
            </GlassSurface>
          </div>
          <div>
            {index < last ? (
              <Button
                variant="glass"
                arrow
                onClick={() => {
                  setIndex(index + 1);
                }}
              >
                {FLOW_101.nextLabel}
              </Button>
            ) : (
              <ButtonLink href={connectHref} variant="primary" arrow>
                {FLOW_101.connectLabel}
              </ButtonLink>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}
