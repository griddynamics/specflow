// AC-7.6 (duplicate), AC-7.7 (non-PDF), AC-12.4 (200 MiB through the BFF with no 4xx/5xx); P17 s07 (FE-BP-09.AC2, D09).
// No agent runs, no parse: the objects are uploaded and deleted by project prefix.
// On the new screens an upload in Mission Control always continues with parse and summarize (paid), so the duplicate and the
// 200 MiB checks stay at the API level, as in the hello spec (P17 traps: "keep the old flow's check as an API-level test"), and
// the UI checks are the free ones: a page opened on uploaded documents shows "Continue" and starts nothing (G01-16, PA18), and a
// non-PDF picked in an Empty project is rejected with "Unsupported file type" (415) without creating a document.
import fs from "node:fs";
import {
  bffHeaders,
  createProject,
  devLogin,
  expect,
  FIXTURE_PDF,
  evidence,
  networkQuiet,
  shot,
  storageCleanup,
  fileBodyStream,
  syntheticPdf,
  test,
  uniqueName,
  uploadInMissionControl,
  defined,
} from "./support";

const APPLICATION_PDF = "application/pdf";

test.describe.configure({ retries: 1 }); // no model cost here, so a retry (with a trace) is cheap

type DocumentStatus = "uploaded" | "processing" | "parsed" | "failed" | "ingested" | "partially_ingested"; // backend DOCUMENT_STATUSES
type SummarizationStatus = "none" | "running" | "succeeded" | "failed"; // backend SUMMARIZATION_STATUSES

interface UploadBody {
  outcome?: string;
  document?: { id: string; original_filename: string; size_bytes: number; status: DocumentStatus };
  error?: { code?: string };
}
interface DocList {
  items: { id: string; status: DocumentStatus; summarization_status: SummarizationStatus }[];
}

test("duplicate, non-PDF and a 200 MiB upload", { tag: "@e2e" }, async ({ page, context, baseURL, track }) => {
  test.setTimeout(15 * 60_000);
  const base = defined(baseURL);
  await devLogin(page);
  const projectId = await createProject(page, uniqueName("e2e-edge"));
  track(projectId);
  const api = `${base}/api/backend/projects/${projectId}/documents`;
  const headers = await bffHeaders(context, base);
  const pdf = fs.readFileSync(FIXTURE_PDF);
  const objects = () => storageCleanup(projectId, true).before;
  const documents = async () => ((await (await context.request.get(api, { headers })).json()) as DocList).items;

  await test.step("AC-7.6 duplicate: same bytes in the same project", async () => {
    const first = await context.request.post(`${api}?filename=extraction.pdf`, {
      headers: { ...headers, "content-type": APPLICATION_PDF },
      data: pdf,
    });
    expect(first.status()).toBe(201);
    const created = (await first.json()) as UploadBody;
    expect(created.document?.status).toBe("uploaded");
    expect(objects()).toBe(1);
    const again = await context.request.post(`${api}?filename=extraction.pdf`, {
      headers: { ...headers, "content-type": APPLICATION_PDF },
      data: pdf,
    });
    expect(again.status()).toBe(200);
    const dup = (await again.json()) as UploadBody;
    expect(dup.outcome).toBe("duplicate");
    expect(dup.document?.id).toBe(created.document?.id);
    expect(objects(), "the duplicate's new object was deleted").toBe(1);
    // The UI on page load: the unfinished document shows "Continue" and nothing is started (no parse, no summary: G01-16).
    // Every parse or summarize POST the page sends from the first request of the load on is recorded.
    const started: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && /\/documents\/[^/]+\/(?:parse|summarize)$/.test(new URL(r.url()).pathname)) started.push(r.url());
    });
    await page.goto(`/projects/${projectId}/mission-control`);
    await expect(page.getByRole("button", { name: "Continue extraction.pdf" })).toBeVisible();
    await expect(page.getByTestId("mc-file-row")).toHaveCount(1);
    await shot(page, "10-duplicate-continue");
    // Once no request is in flight for QUIET_MS, the page sent no parse or summarize POST at all (recorded since before the
    // goto), and the backend still holds the document as uploaded with no summary (nothing was started on the server either).
    await networkQuiet(page);
    expect(started, "opening the page started no parse or summary").toEqual([]);
    const docs = await documents();
    expect(docs).toHaveLength(1);
    expect(docs[0].status, "opening the page started no parse").toBe("uploaded");
    expect(docs[0].summarization_status).toBe("none");
    evidence({
      event: "edge.duplicate",
      project_id: projectId,
      first: first.status(),
      second: again.status(),
      outcome: dup.outcome,
      objects: 1,
    });
  });

  await test.step("AC-7.7 non-PDF: wrong type and wrong magic bytes give 415", async () => {
    const wrongType = await context.request.post(`${api}?filename=notes.txt`, {
      headers: { ...headers, "content-type": "text/plain" },
      data: "hello",
    });
    expect(wrongType.status()).toBe(415);
    expect(((await wrongType.json()) as UploadBody).error?.code).toBe("unsupported_format");
    const wrongMagic = await context.request.post(`${api}?filename=fake.pdf`, {
      headers: { ...headers, "content-type": APPLICATION_PDF },
      data: "this is not a pdf",
    });
    expect(wrongMagic.status()).toBe(415);
    expect(objects(), "rejected uploads leave no object").toBe(1);
    // The same through the UI, in a second (Empty) project: the app always labels the body application/pdf, so the magic check
    // answers 415 and the row says "Unsupported file type" with Remove (not Retry). No document, no object, no parse.
    const emptyId = await createProject(page, uniqueName("e2e-edge-nonpdf"));
    track(emptyId);
    await uploadInMissionControl(page, { name: "notes.pdf", mimeType: APPLICATION_PDF, buffer: Buffer.from("plain text, not a pdf") });
    const row = page.getByTestId("mc-file-row");
    await expect(row).toHaveAttribute("data-state", "failed");
    await expect(row.getByText("Unsupported file type", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Remove notes.pdf" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry notes.pdf" })).toHaveCount(0);
    await shot(page, "11-non-pdf-rejected");
    const other = await context.request.get(`${base}/api/backend/projects/${emptyId}/documents`, { headers });
    expect(((await other.json()) as DocList).items).toHaveLength(0);
    expect(storageCleanup(emptyId, true).before, "the rejected upload left no object").toBe(0);
    evidence({
      event: "edge.non_pdf",
      project_id: projectId,
      wrong_type: wrongType.status(),
      wrong_magic: wrongMagic.status(),
      objects: 1,
    });
  });

  await test.step("AC-12.4 a 200 MiB upload through the BFF succeeds", async () => {
    const size = 200 * 1024 * 1024;
    const big = await syntheticPdf(size);
    try {
      const started = Date.now();
      const init: RequestInit & { duplex: "half" } = {
        method: "POST",
        headers: { ...headers, "content-type": APPLICATION_PDF, "content-length": String(size) },
        body: fileBodyStream(big.file),
        duplex: "half",
        signal: AbortSignal.timeout(10 * 60_000),
      };
      const res = await fetch(`${api}?filename=big.pdf`, init);
      const body = (await res.json()) as UploadBody;
      expect(res.status, "200 MiB upload status").toBe(201);
      expect(body.document?.size_bytes).toBe(size);
      expect(objects()).toBe(2);
      evidence({
        event: "edge.big_upload",
        project_id: projectId,
        status: res.status,
        size_bytes: size,
        seconds: Math.round((Date.now() - started) / 1000),
        objects: 2,
      });
    } finally {
      big.remove();
    }
    // Both documents are listed in Mission Control as unfinished ("Continue"), and nothing is started.
    await page.goto(`/projects/${projectId}/mission-control`);
    await expect(page.getByTestId("mc-file-row")).toHaveCount(2);
    await expect(page.getByRole("button", { name: "Continue big.pdf" })).toBeVisible();
    await shot(page, "12-big-upload-listed");
    expect(
      (await documents()).every((d) => d.status === "uploaded"),
      "no parse was started",
    ).toBe(true);
  });
});
