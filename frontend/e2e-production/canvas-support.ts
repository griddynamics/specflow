// The helpers the canvas specs share (canvas-production.spec.ts, canvas-connectors-production.spec.ts): locators of the hubs, wires, nodes and
// dialogs, a project with a parsed document (what lets the graph be built), a signed-in canvas of a fresh project, and the file list of a file link
// form (Box, Dropbox).
import { randomBytes } from "node:crypto";
import type { Locator, Page } from "@playwright/test";
import { bff, expect, loginV2, seedDocument, seedMembers, type RequestLog } from "./support";

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

/** What a file link form (Box, Dropbox) is checked with: the app (`box`), its name on the node (`Box`), its `folder` link (the stub lists 12 files) and
 * `big` link (1000 files, capped), the `exclude` ids of files 2 and 5, the `href` the node opens, and `shot` for the screenshots (`<app>-form-*`). */
export interface FileLinkCase {
  app: string;
  name: string;
  folder: string;
  big: string;
  exclude: string[];
  href: string | RegExp;
  shot: (page: Page, name: string) => Promise<void>;
}

/** The file list of a file link form: the check lists every file checked, a new link is a new check, a capped folder says so, unchecking, Select none
 * (nothing to add) and Select all, then Add source sends the unchecked ids as `exclude` and the node, once imported, opens the link in the app. */
export async function checkPickAndAdd(
  page: Page,
  form: Locator,
  requests: readonly RequestLog[],
  pid: string,
  c: FileLinkCase,
): Promise<void> {
  const link = form.getByRole("textbox");
  const checkLink = form.getByRole("button", { name: "Check link" });
  await link.fill(c.folder);
  await checkLink.click();
  await expect(form.getByText("Folder Project files · 12 files (1.2 MB), 3 skipped (other types), 2 folders.")).toBeVisible();
  await expect(form.getByRole("alert")).toHaveCount(0);
  const list = form.getByRole("group", { name: "Files to add" });
  await expect(list.getByRole("checkbox")).toHaveCount(12);
  await expect(list.getByRole("checkbox", { checked: true })).toHaveCount(12);
  await c.shot(page, `${c.app}-form-checked`);
  await link.fill(`${c.folder}/`); // a different link is a new check
  await expect(checkLink).toBeVisible();
  await link.fill(c.big);
  await checkLink.click();
  await expect(form.getByText(/Folder Big archive · 1000 files \(.*\), 20 skipped \(other types\), 300 folders\./)).toBeVisible();
  await expect(form.getByText(/more files than one import takes/)).toBeVisible();
  await c.shot(page, `${c.app}-form-capped`);
  await link.fill(c.folder);
  await checkLink.click();
  await expect(form.getByText(/^Folder Project files · 12 files/)).toBeVisible();
  await list.getByRole("checkbox", { name: /File 2\.docx/ }).uncheck();
  await list.getByRole("checkbox", { name: /File 5\.html/ }).uncheck();
  await expect(form.getByText("10 of 12 files")).toBeVisible();
  await c.shot(page, `${c.app}-form-unchecked`);
  await form.getByRole("button", { name: "Select none" }).click();
  await expect(form.getByText("No files")).toBeVisible();
  await expect(form.getByRole("button", { name: ADD.source })).toBeDisabled(); // nothing to add
  await form.getByRole("button", { name: "Select all" }).click();
  await list.getByRole("checkbox", { name: /File 2\.docx/ }).uncheck();
  await list.getByRole("checkbox", { name: /File 5\.html/ }).uncheck();
  await form.getByRole("button", { name: ADD.source }).click();
  await expect(form).toBeHidden();
  const node = nodes(page).filter({ hasText: "Project files" });
  await expect(node).toHaveAttribute(STATE, "busy");
  await expect(node).toHaveAttribute(STATE, "on", { timeout: 15_000 });
  await expect(node).toContainText(`13 files · ${c.name}`); // 12 listed - 2 left out + 3 skipped
  const adds = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/${c.app}`);
  expect(bodiesOf(adds)).toEqual([{ link: c.folder, exclude: c.exclude }]);
  await node.getByRole("button").click();
  const detail = dialog(page, "Project files");
  await expect(detail.getByText(/^13 files\. Imported when it was added/)).toBeVisible();
  await expect(detail.getByRole("link", { name: `Open in ${c.name}` })).toHaveAttribute("href", c.href);
  await expect(detail.getByRole("button", { name: "Remove" })).toBeVisible();
}
