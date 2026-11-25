// The plus dialog: the connections of the stage whose plus was pressed first, the others under "Connections for the other stages" (dashed, so
// people learn what fits where). A connection whose app is not connected on the project says "Set up first" and goes to Integrations, which
// returns here with its form open. MCP is automatic: it is not listed.
import { ConnectorCard, ConnectorGrid, Disclosure, Notice } from "@graphico/ui";
import type { ConnectorSpec } from "../connector-types";
import { CANVAS_COPY } from "./canvas-copy";
import type { ZoneId } from "./canvas-types";

export interface AddListProps {
  zone: ZoneId;
  connectors: readonly ConnectorSpec[];
  /** Whether the connection's app works on this project; kinds with no app are always usable. */
  usable: (spec: ConnectorSpec) => boolean;
  appName: (spec: ConnectorSpec) => string;
  onPick: (spec: ConnectorSpec) => void;
  onSetUp: (spec: ConnectorSpec) => void;
}

export function AddList({ zone, connectors, usable, appName, onPick, onSetUp }: Readonly<AddListProps>) {
  const listed = connectors.filter((c) => c.zone !== null);
  const mine = listed.filter((c) => c.zone === zone);
  const others = listed.filter((c) => c.zone !== zone);
  return (
    <>
      <Notice>{CANVAS_COPY.addIntro[zone]}</Notice>
      {mine.length === 0 ? (
        <Notice>{CANVAS_COPY.nothingYet}</Notice>
      ) : (
        <ConnectorGrid>
          {mine.map((spec) => {
            const ready = usable(spec);
            return (
              <ConnectorCard
                key={spec.kind}
                mark={spec.mark}
                name={spec.name}
                line={ready ? spec.line : CANVAS_COPY.setUpLine.replace("{app}", appName(spec))}
                tag={ready ? CANVAS_COPY.available : CANVAS_COPY.setUpFirst}
                onPress={() => {
                  if (ready) onPick(spec);
                  else onSetUp(spec);
                }}
              />
            );
          })}
        </ConnectorGrid>
      )}
      {others.length === 0 ? null : (
        <Disclosure summary={CANVAS_COPY.others}>
          <ConnectorGrid>
            {others.map((spec) => (
              <ConnectorCard
                key={spec.kind}
                mark={spec.mark}
                name={spec.name}
                line={spec.line}
                tag={`${CANVAS_COPY.forStage} ${CANVAS_COPY.zoneName[spec.zone ?? zone]}`}
                disabled
                onPress={() => undefined}
              />
            ))}
          </ConnectorGrid>
        </Disclosure>
      )}
    </>
  );
}
