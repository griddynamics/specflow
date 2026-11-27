// The cards of the Integrations page, one per app. Later sessions that add a connector to an existing app change its Setup component;
// a new app is one more entry here and in `lib/domain/integrations.ts` (Box is the first app added after V2: D20, `docs/box/`).
import type { ComponentType } from "react";
import type { VendorKind } from "@graphico/ui";
import type { IntegrationApp } from "@/lib/domain/integrations";
import { AtlassianSetup } from "./AtlassianSetup";
import { BoxSetup } from "./BoxSetup";
import { DropboxSetup } from "./DropboxSetup";
import { GitHubSetup } from "./GitHubSetup";
import { LinearSetup } from "./LinearSetup";
import { SlackSetup } from "./SlackSetup";
import type { SetupProps } from "./setup-props";

export interface CardSpec {
  app: IntegrationApp;
  marks: readonly VendorKind[];
  /** The body of the app's set-up dialog. */
  Setup: ComponentType<SetupProps>;
}

export const INTEGRATION_CARDS: readonly CardSpec[] = [
  { app: "atlassian", marks: ["jira", "confluence"], Setup: AtlassianSetup },
  { app: "github", marks: ["github"], Setup: GitHubSetup },
  { app: "slack", marks: ["slack"], Setup: SlackSetup },
  { app: "linear", marks: ["linear"], Setup: LinearSetup },
  { app: "box", marks: ["box"], Setup: BoxSetup },
  { app: "dropbox", marks: ["dropbox"], Setup: DropboxSetup },
];
