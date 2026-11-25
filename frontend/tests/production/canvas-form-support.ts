// Not a test: what the server-rendered tests of the canvas forms share: the page context with no ports, the render, and the props a form gets.
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ConnectorKind, ZoneId } from "@/components/v2/canvas/canvas-types";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";

export const noop = () => undefined;
const noPorts: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: noPorts as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };

/** The markup of a form rendered once on the server, with no port answering. */
export const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

/** The props the plus dialog gives the form of a registered kind. */
export const formProps = (zone: ZoneId, kind: ConnectorKind): FormProps => ({
  projectId: "p1",
  zone,
  spec: defined(CONNECTORS.find((c) => c.kind === kind)),
  mcp: { mcpUrl: "", clientId: "", serverName: "" },
  reload: { current: noop },
  onBack: noop,
  onClose: noop,
});
