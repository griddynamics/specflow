// The shape of a registered connection (06a): what the plus dialog lists, what it needs first, the form that adds it and the mapper that
// turns the ports' answers into its nodes. `connectors.ts` holds the list; a session that adds a kind (GitHub repository, Slack channel,
// Jira, ...) adds one spec there, its form component and its node mapper, and nothing else on the canvas.
import type { ComponentType, RefObject } from "react";
import type { VendorKind } from "@graphico/ui";
import type { IntegrationApp } from "@/lib/domain/integrations";
import type { ConnectorKind, NodeMapper, ZoneId } from "./canvas/canvas-types";

/** The labels of the check a form runs before it adds the connection ("Test connection", "Testing…", "Add source"). */
export interface CheckLabels {
  test: string;
  testing: string;
  add: string;
}

/** The values the MCP dialog needs from the deployment (the page reads them on the server: `guideConfig()`). */
export interface McpAddress {
  mcpUrl: string;
  clientId: string;
  serverName: string;
}

export interface FormProps {
  projectId: string;
  /** The stage whose plus (or node) opened the form. */
  zone: ZoneId;
  spec: ConnectorSpec;
  mcp: McpAddress;
  /** Reads the project's status again (the sources come from there). */
  reload: RefObject<() => void>;
  /** Back to the plus dialog's list. */
  onBack: () => void;
  onClose: () => void;
}

export interface ConnectorSpec {
  kind: ConnectorKind;
  /** The stage whose plus lists it first; `null` = automatic (MCP): it is not in a list, it joins by itself. */
  zone: ZoneId | null;
  name: string;
  /** One line under the name in the plus dialog. */
  line: string;
  mark: VendorKind;
  /** The integration it needs on the project first; `null` = none (Upload, Google Drive, MCP). */
  app: IntegrationApp | null;
  /** The form's dialog title. */
  title: string;
  /** The check before adding; `null` = none. */
  check: CheckLabels | null;
  Form: ComponentType<FormProps>;
  /** Its nodes on the canvas. */
  nodes: NodeMapper;
}
