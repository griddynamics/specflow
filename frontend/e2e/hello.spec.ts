// AC-16.6, P17 s07 (FE-BP-09.AC2, D09, R11): the hello flow on the NEW production screens against the real stack:
// dev login -> New project (name only) -> upload the fixture in Mission Control -> parse and summarize run on their own ->
// Ready -> summary (read through the API: real document contents have no screen, G01-13) -> Langfuse trace
// (AC-11.3) -> sign out. One real agent run per execution: run it only with a Haiku model (D13) and never with retries.
import {
  bffHeaders,
  createProject,
  devLogin,
  expect,
  FIXTURE_PDF,
  evidence,
  Langfuse,
  shot,
  signOut,
  storageCleanup,
  test,
  uniqueName,
  uploadInMissionControl,
  waitForReady,
  defined,
} from "./support";

const PARSE_TIMEOUT = 10 * 60_000;
const SUMMARIZE_TIMEOUT = 15 * 60_000;

type DocumentStatus = "uploaded" | "processing" | "parsed" | "failed" | "ingested" | "partially_ingested"; // backend DOCUMENT_STATUSES
type SummarizationStatus = "none" | "running" | "succeeded" | "failed"; // backend SUMMARIZATION_STATUSES

interface DocRow {
  id: string;
  status: DocumentStatus;
  summarization_status: SummarizationStatus;
  summarization: string | null;
}

/** The short text of a thrown value: an Error's first message line, anything else as a string. */
const problemText = (e: unknown): string => (e instanceof Error ? e.message.split("\n")[0] : String(e));

test("hello: upload, parse, summarize, trace, sign out", { tag: "@e2e" }, async ({ page, context, baseURL, track }, testInfo) => {
  test.setTimeout(PARSE_TIMEOUT + SUMMARIZE_TIMEOUT + 5 * 60_000);
  const base = defined(baseURL);

  await test.step("dev login", async () => {
    await devLogin(page);
    await shot(page, "01-after-login");
  });

  const name = uniqueName("e2e-hello");
  let projectId = "";

  await test.step("create a uniquely named project (New, name only)", async () => {
    projectId = await createProject(page, name);
    track(projectId);
    expect(storageCleanup(projectId, true).before, "no objects before the upload").toBe(0);
    await expect(page.getByTestId("topbar-project-name")).toHaveText(name);
    await shot(page, "02-project-created");
  });

  const startedAt = new Date();

  await test.step("upload extraction.pdf in Mission Control; parse and summarize run without a click; Ready", async () => {
    await uploadInMissionControl(page, FIXTURE_PDF);
    await expect(page.getByTestId("mc-processing")).toBeVisible();
    await expect(page.getByTestId("mc-file-row")).toHaveCount(1);
    await shot(page, "03-processing");
    await waitForReady(page, PARSE_TIMEOUT + SUMMARIZE_TIMEOUT);
    await expect(page.getByTestId("view-readme")).toBeVisible(); // Ready views are shown
    await shot(page, "04-ready");
    const counts = storageCleanup(projectId, true);
    evidence({ event: "storage.after_upload", project_id: projectId, objects: counts.before });
    expect(counts.before, "one object after the upload").toBe(1);
  });

  await test.step("summary through the API; Langfuse trace", async () => {
    const headers = await bffHeaders(context, base);
    const list = await context.request.get(`${base}/api/backend/projects/${projectId}/documents`, { headers });
    expect(list.status()).toBe(200);
    const items = ((await list.json()) as { items: DocRow[] }).items;
    expect(items).toHaveLength(1);
    const documentId = items[0].id;
    const res = await context.request.get(`${base}/api/backend/projects/${projectId}/documents/${documentId}`, { headers });
    const doc = (await res.json()) as DocRow;
    expect(doc.status, "parsed (the backend may move it on to ingested)").toMatch(/^(parsed|ingested|partially_ingested)$/);
    expect(doc.summarization_status).toBe("succeeded");
    expect(doc.summarization ?? "", "summary text").not.toBe("");
    const summaryChars = (doc.summarization ?? "").length;

    // AC-11.3: the trace of this workflow (id = doc-summary-<document id>) in the local Langfuse.
    const facts = await new Langfuse().traceFor(`doc-summary-${documentId}`, startedAt);
    evidence({ event: "langfuse.trace", project_id: projectId, document_id: documentId, summary_chars: summaryChars, ...facts });
    testInfo.annotations.push({
      type: "trace",
      description: `${facts.traceId} generations=${String(facts.generations)} tools=${facts.toolNames.join(",")}`,
    });
    expect(facts.generations, "GENERATION rows with input usage").toBeGreaterThanOrEqual(1);
    expect(facts.toolNames).toEqual(expect.arrayContaining(["read_document", "submit_summary"]));
    expect(facts.agentNames).toEqual(expect.arrayContaining(["summarize-document", "document-summarizer"]));
    expect(facts.secretInIo, "a secret value found in the trace I/O").toBe(false);
  });

  // Review m1 (paid-run safety): everything paid is checked and its evidence written above. The sign-out step below is tolerant:
  // a failure there is recorded (evidence + annotation) but never fails the run, so the paid summary is never repeated for it.
  testInfo.annotations.push({
    type: "paid-run",
    description:
      "The paid steps (upload, parse, summarize, trace) are done. If only the sign-out step reports a problem, do NOT re-run this spec: the sign-out is covered without model calls by npm run e2e:production.",
  });

  await test.step("sign out (tolerant: never fails the paid run)", async () => {
    const problems: string[] = []; // empty, or the one failure of the sign-out checks
    try {
      await signOut(page);
      await expect(page.getByTestId("login-dev-submit")).toBeVisible();
      await shot(page, "05-signed-out");
      await page.goto("/");
      await expect(page).toHaveURL(/\/login/); // the session is really gone
    } catch (e) {
      problems.push(problemText(e));
    }
    evidence({ event: "signout", project_id: projectId, ok: problems.length === 0 });
    testInfo.annotations.push(
      ...problems.map((problem) => ({ type: "signout-problem", description: `${problem} (no re-run needed: the paid steps passed)` })),
    );
  });
});
