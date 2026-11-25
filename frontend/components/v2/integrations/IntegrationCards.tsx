// The cards of the Integrations page and the list of what is always on under them. Props only: the page owns the data and the dialog.
import { BuiltInList, IntegrationCard, IntegrationGrid } from "@graphico/ui";
import type { Integration, IntegrationApp } from "@/lib/domain/integrations";
import { V2_COPY } from "../v2-copy";
import { cardViewOf } from "./integration-logic";
import { INTEGRATION_CARDS } from "./registry";

const BUILT_IN = [
  { key: "upload", mark: "upload", ...V2_COPY.builtIn.upload },
  { key: "mcp", mark: "mcp", ...V2_COPY.builtIn.mcp },
] as const;

export function IntegrationCards({ rows, onOpen }: Readonly<{ rows: readonly Integration[]; onOpen: (app: IntegrationApp) => void }>) {
  return (
    <>
      <IntegrationGrid label={V2_COPY.integrationsLabel}>
        {INTEGRATION_CARDS.map((card) => {
          const row = rows.find((r) => r.app === card.app);
          if (row === undefined) return null;
          const view = cardViewOf(row);
          return (
            <IntegrationCard
              key={card.app}
              marks={card.marks}
              name={V2_COPY.appNames[card.app]}
              sub={V2_COPY.appSubs[card.app]}
              state={view.state}
              badge={view.badge}
              by={view.by}
              actionLabel={view.actionLabel}
              onAction={() => {
                onOpen(card.app);
              }}
            />
          );
        })}
      </IntegrationGrid>
      <BuiltInList heading={V2_COPY.builtInHeading} items={BUILT_IN} />
    </>
  );
}
