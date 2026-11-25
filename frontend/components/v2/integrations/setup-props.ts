import type { Integration } from "@/lib/domain/integrations";

/** What the Integrations page hands the set-up dialog of one app. */
export interface SetupProps {
  projectId: string;
  integration: Integration;
  /** The caller may disconnect it (the member who connected it, or the owner; Slack: any member). */
  canDisconnect: boolean;
  /** Where an app's sign-in or install page returns to: this page, with the app's own dialog open and the `next` path kept. */
  returnTo: string;
  /** Binds the caller's own connection to the project (Atlassian, GitHub). */
  onConnect: () => Promise<boolean>;
  onDisconnect: () => Promise<boolean>;
  /** The page reads the integrations again (the Slack dialog asks while it waits for the channel). */
  onRefresh: () => void;
  /** A failed call: the page says it in a toast, or goes home when the project is gone. */
  onError: (e: unknown) => void;
  /** Closes the dialog. */
  onClose: () => void;
  /** Done: closes the dialog, or follows `next` back to the canvas form that asked. */
  onDone: () => void;
}
