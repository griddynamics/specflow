// The helpers the canvas specs share (canvas-production.spec.ts, canvas-connectors-production.spec.ts): locators of the hubs, wires, nodes and
// dialogs, a project with a parsed document (what lets the graph be built), and a signed-in canvas of a fresh project.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { bff, expect, loginV2, seedDocument, seedMembers } from "./support";

export const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
export const STATE = "data-state";
export const YELLOW = "data-yellow";
export const MOTION = "data-motion";
export const SPECS = "Specs folder";
export const ADD = { source: "Add source", channel: "Add channel", destination: "Add destination" } as const;
export const FOLDER = "https://drive.google.com/drive/folders/fake-synthetic-30";
export const done = (n: number) => Array.from({ length: n }, (_, i) => ({ name: `doc-${String(i + 1)}.pdf`, state: "done" }));

/** The JSON bodies of the logged requests. */
export const bodiesOf = (log: readonly { postData: string | null }[]): unknown[] =>
  log.map((r) => JSON.parse(r.postData ?? "{}") as unknown);

export const hub = (page: Page, id: "graph" | "gap" | "plan") => page.getByTestId(`hub-${id}`);
export const wire = (page: Page, key: string) => page.getByTestId(`wire-${key}`);
export const nodes = (page: Page) => page.getByTestId("canvas-node");
export const dialog = (page: Page, name: string | RegExp) => page.getByRole("dialog", { name });

/** The project has one parsed document, which is what lets the graph be built (the sources API counts documents, not the seeded files). */
export async function parsed(page: Page, pid: string): Promise<void> {
  await seedDocument(page, pid, "spec.pdf");
  await expect
    .poll(
      async () =>
        ((await bff(page, "GET", `/projects/${pid}/sources?batches=1`)).json as { processed_documents: number }).processed_documents,
    )
    .toBeGreaterThan(0);
}

export async function canvas(page: Page, name: string): Promise<string> {
  await loginV2(page);
  const { id } = await seedMembers({ name });
  await page.goto(`/projects/${id}`);
  await expect(page.getByRole("main", { name: "Mission Control canvas" })).toBeVisible();
  return id;
}
