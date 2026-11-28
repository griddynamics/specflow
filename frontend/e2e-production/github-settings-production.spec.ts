// GitHub repositories (docs/github/INDEX.md) on the PRODUCTION routes in a real browser, against the stub API
// (tests/support/stub-github.mjs) through the real BFF: the V2 Integrations dialog and the canvas node. GitHub's own pages are intercepted
// and never reached: the app's install page answers with the redirect to the setup route, which sends only the signed state to the API.
// A repository picked in the dialog clones (the row says the word, the node dialog the facts); a failed clone says its reason; a repository
// is removed from its node after a confirmation. The connect flow, the dialog's picker add/remove and the read-only member form live in
// v2-production and canvas-connectors; the canvas form's own add in canvas-connectors.
import type { Page } from "@playwright/test";
import { canvas, dialog, nodes, STATE, unique } from "./canvas-support";
import { allowOrigin, expect, seedGitHub, seedIntegrations, stubLog, test } from "./support";

const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");
const INTEGRATIONS = (pid: string) => `/projects/${pid}/integrations`;
const ALEX = { user_id: "someone", name: "Sam Ortiz" };
const ACTIVE = { status: "active", login: "octo-dev" } as const;
const card = (page: Page, name: string) => page.getByRole("listitem").filter({ has: page.getByRole("heading", { name, exact: true }) });

/** Answers every github.com request of `page` with a redirect to `to(url)` (the page GitHub would send the browser to). */
async function interceptGitHub(page: Page, to: (url: URL) => string): Promise<URL[]> {
  const seen: URL[] = [];
  allowOrigin(page, "https://github.com");
  await page.route(
    (url) => url.origin === "https://github.com",
    async (route) => {
      const url = new URL(route.request().url());
      seen.push(url);
      await route.fulfill({ status: 302, headers: { location: to(url) } });
    },
  );
  return seen;
}

test.describe("GitHub on the V2 Integrations page and canvas", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await seedGitHub({ reset: true });
    await seedIntegrations({ reset: true });
  });

  test("Choose repositories on GitHub goes to the install page; the setup return sends only the state, toasts once and opens the dialog again", async ({
    page,
    requests,
    baseURL,
  }) => {
    const pid = await canvas(page, unique("GitHub install"));
    await seedGitHub({ connection: ACTIVE });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    const seen = await interceptGitHub(
      page,
      (url) =>
        `${baseURL ?? ""}/api/integrations/github/setup?installation_id=4242&setup_action=install&state=${url.searchParams.get("state") ?? ""}`,
    );
    const logged = (await stubLog()).length;
    await page.goto(INTEGRATIONS(pid));
    await card(page, "GitHub").getByRole("button", { name: "Manage" }).click();
    await dialog(page, "GitHub").getByRole("button", { name: "Choose repositories on GitHub" }).click();

    await expect(page.getByTestId("toast").filter({ hasText: "GitHub repositories updated" })).toHaveCount(1);
    expect(seen).toHaveLength(1);
    expect(seen[0].pathname).toMatch(/^\/apps\/[^/]+\/installations\/new$/);
    const install = requests.find((r) => r.method === "POST" && r.path === "/api/backend/integrations/github/install");
    expect(bodyOf(install ?? { postData: null })).toEqual({ return_to: `${INTEGRATIONS(pid)}?setup=github` });
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/integrations\\?setup=github$`));
    const setup = (await stubLog()).slice(logged).filter((e) => e.path === "/api/v1/integrations/github/setup");
    expect(setup).toHaveLength(1);
    expect(setup[0].path).not.toContain("4242"); // the installation_id is never forwarded; only the signed state is
    await expect(dialog(page, "GitHub")).toBeVisible(); // `?setup=github` opens the dialog again
  });

  test("a repository picked in the Integrations dialog clones: Waiting on its row, Cloned on the canvas node, its facts in the node dialog", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("GitHub clone"));
    await seedGitHub({ connection: ACTIVE, cloneMs: 1500 });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    await page.goto(INTEGRATIONS(pid));
    await card(page, "GitHub").getByRole("button", { name: "Manage" }).click();
    const set = dialog(page, "GitHub");
    await set.getByRole("checkbox", { name: "acme/payments-api" }).click(); // checked once the request is answered
    await expect(set.getByRole("checkbox", { name: /acme\/payments-api/ })).toBeChecked();
    const adds = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/repositories`);
    expect(adds.map(bodyOf)).toEqual([{ github_repo_id: 101 }]);
    const row = set.locator("label").filter({ hasText: "acme/payments-api" });
    await expect(row).toContainText("Waiting"); // the dialog polls the list while the check runs
    await expect(row).toContainText("Cloned", { timeout: 15_000 });
    await set.getByRole("button", { name: "Done" }).click();

    await page.goto(`/projects/${pid}`);
    const node = nodes(page).filter({ hasText: "payments-api" });
    await expect(node).toHaveAttribute(STATE, "on");
    await expect(node).toContainText("Cloned · GitHub");
    await node.getByRole("button").click();
    const detail = dialog(page, "payments-api");
    await expect(detail).toContainText("Cloned. 3f9c2e1 · 1,204 files · 3.2 MB. The build does not read repositories yet.");
  });

  test("a repository is removed from its node after the confirmation; Keep asks nothing of the API", async ({ page, requests }) => {
    const pid = await canvas(page, unique("GitHub remove"));
    await seedGitHub({ pid, repositories: [{ full_name: "acme/web-app" }] });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}`);
    const node = nodes(page).filter({ hasText: "web-app" });
    await expect(node).toHaveAttribute(STATE, "on");
    await node.getByRole("button").click();
    const detail = dialog(page, "web-app");
    await detail.getByRole("button", { name: "Remove" }).click();
    const confirm = page.getByRole("alertdialog", { name: "Remove this repository?" });
    await expect(confirm).toContainText("The clone is deleted. The graph does not use it.");
    await confirm.getByRole("button", { name: "Keep" }).click();
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await detail.getByRole("button", { name: "Remove" }).click();
    await page.getByRole("alertdialog", { name: "Remove this repository?" }).getByRole("button", { name: "Remove" }).click();
    await expect(detail).toBeHidden();
    await expect(nodes(page).filter({ hasText: "web-app" })).toHaveCount(0);
    expect(
      requests.filter((r) => r.method === "DELETE" && r.path.startsWith(`/api/backend/projects/${pid}/repositories/`)),
    ).toHaveLength(1);
  });

  test("a repository whose clone failed is a stopped node that says its reason in the dialog", async ({ page }) => {
    const pid = await canvas(page, unique("GitHub failed"));
    await seedGitHub({
      pid,
      repositories: [{ full_name: "acme/web-app", clone_state: "failed", failure_code: "github_repo_not_accessible", added_by_me: false }],
    });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}`);
    const node = nodes(page).filter({ hasText: "web-app" });
    await expect(node).toHaveAttribute(STATE, "stopped");
    await expect(node).toContainText("Failed");
    await node.getByRole("button").click();
    await expect(dialog(page, "web-app")).toContainText(
      "GitHub refused access. The repository was removed from the Specflow app or the person who added it lost access.",
    );
  });
});
