// AC-12.4: a 200 MiB synthetic PDF goes BFF -> counting stub upstream with no 4xx/5xx,
// and the frontend's peak RSS minus its warm baseline stays under a FIXED 128 MiB gate.
// Measured on the built standalone server (never `next dev`), loopback-speed and slow
// upstream cases, fresh process per run. The real API + GCS leg runs in S11/S13.
// Ports 3230-3239. The 200 MiB file is generated here and deleted afterwards.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { frontendEnv, randomSecret, rssKiB, startFrontend } from "../support/servers.mjs";
import { startCountingUpstream } from "../support/counting-upstream.mjs";
import { devLogin } from "../support/session.mjs";

const GATE_MIB = 128; // fixed (C28): a miss is reported to the owner, never relaxed
const SIZE_MIB = 200;
const FE_PORT = 3230;
const UP_PORT = 3231;
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "specflow-fe-upload-"));
const bigFile = path.join(dir, "synthetic-200mib.pdf");
let bigSha;
const results = [];

after(() => {
  fs.rmSync(dir, { recursive: true, force: true });
  console.log("rss.results " + JSON.stringify(results));
});

function generate() {
  if (bigSha) return;
  const chunk = crypto.randomBytes(1024 * 1024);
  const hash = crypto.createHash("sha256");
  const fd = fs.openSync(bigFile, "w");
  const head = Buffer.from("%PDF-1.7\n");
  fs.writeSync(fd, head);
  hash.update(head);
  for (let i = 0; i < SIZE_MIB; i++) {
    fs.writeSync(fd, chunk);
    hash.update(chunk);
  }
  fs.closeSync(fd);
  bigSha = hash.digest("hex");
}

function post({ base, cookie, file, abortAfterMs }) {
  const { hostname, port } = new URL(base);
  const size = fs.statSync(file).size;
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname,
        port,
        method: "POST",
        path: "/api/backend/projects/p1/documents?filename=big.pdf",
        headers: {
          cookie,
          origin: base,
          "x-requested-with": "specflow",
          "content-type": "application/pdf",
          "content-length": size,
        },
      },
      (res) => {
        let text = "";
        res.on("data", (c) => (text += c));
        res.on("end", () => resolve({ status: res.statusCode, text, size }));
      },
    );
    req.on("error", (e) => (abortAfterMs ? resolve({ status: 0, aborted: true, size }) : reject(e)));
    const stream = fs.createReadStream(file);
    stream.pipe(req);
    if (abortAfterMs) setTimeout(() => req.destroy(), abortAfterMs);
  });
}

/** One fresh frontend + upstream: warm up, baseline, upload, sample RSS. */
async function scenario({ label, throttleMiBps }) {
  const upstream = await startCountingUpstream({ port: UP_PORT, throttleMiBps });
  const fe = await startFrontend({
    port: FE_PORT,
    env: frontendEnv(FE_PORT, {
      APP_ENV: "local",
      AUTH_MODE: "dev",
      API_INTERNAL_URL: upstream.url,
      SESSION_SECRET: randomSecret(),
      AUTH_DEV_SIGNING_KEY: randomSecret(),
    }),
  });
  try {
    const jar = await devLogin(fe.base, "rss.user@example.test");
    const warmFile = path.join(dir, "warm.pdf");
    fs.writeFileSync(warmFile, Buffer.concat([Buffer.from("%PDF-1.7\n"), crypto.randomBytes(1024 * 1024)]));
    const warm = await post({ base: fe.base, cookie: jar.header(), file: warmFile });
    assert.equal(warm.status, 201);
    await new Promise((r) => setTimeout(r, 1500));
    const baseline = rssKiB(fe.pid);

    let peak = baseline;
    const timer = setInterval(() => (peak = Math.max(peak, rssKiB(fe.pid))), 100);
    const res = await post({ base: fe.base, cookie: jar.header(), file: bigFile });
    clearInterval(timer);
    peak = Math.max(peak, rssKiB(fe.pid));

    const sent = upstream.uploads.at(-1);
    assert.ok(res.status >= 200 && res.status < 300, `status ${res.status}`);
    assert.equal(sent.bytes, res.size);
    assert.equal(sent.sha256, bigSha);
    assert.equal(sent.headers["content-type"], "application/pdf");
    assert.match(sent.headers.authorization, /^Bearer /);
    assert.ok(!("cookie" in sent.headers));
    const deltaMiB = (peak - baseline) / 1024;
    results.push({ label, baselineMiB: Math.round(baseline / 1024), peakMiB: Math.round(peak / 1024), deltaMiB: Math.round(deltaMiB) });
    assert.ok(deltaMiB < GATE_MIB, `${label}: peak RSS minus baseline ${deltaMiB.toFixed(0)} MiB must be < ${GATE_MIB}`);
  } finally {
    await fe.stop();
    await upstream.close();
  }
}

test("AC-12.4 setup: generate the synthetic 200 MiB PDF", () => {
  generate();
  assert.equal(fs.statSync(bigFile).size, SIZE_MIB * 1024 * 1024 + 9);
});

for (const i of [1, 2, 3]) {
  test(`AC-12.4: 200 MiB, loopback-speed upstream, fresh process (run ${i})`, () => scenario({ label: `fast-${i}` }));
}
for (const i of [1, 2]) {
  test(`AC-12.4: 200 MiB, slow upstream 20 MiB/s, fresh process (run ${i})`, { timeout: 120_000 }, () =>
    scenario({ label: `slow20-${i}`, throttleMiBps: 20 }),
  );
}

test("AC-6.6: a client abort mid-upload destroys the upstream request", async () => {
  const upstream = await startCountingUpstream({ port: UP_PORT, throttleMiBps: 5 });
  const fe = await startFrontend({
    port: FE_PORT,
    env: frontendEnv(FE_PORT, {
      APP_ENV: "local",
      AUTH_MODE: "dev",
      API_INTERNAL_URL: upstream.url,
      SESSION_SECRET: randomSecret(),
      AUTH_DEV_SIGNING_KEY: randomSecret(),
    }),
  });
  try {
    generate();
    const jar = await devLogin(fe.base, "abort.user@example.test");
    await post({ base: fe.base, cookie: jar.header(), file: bigFile, abortAfterMs: 1500 });
    const deadline = Date.now() + 8000;
    while (!upstream.uploads.at(-1)?.aborted && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
    const rec = upstream.uploads.at(-1);
    assert.ok(rec?.aborted, "the API side saw the abort");
    assert.ok(rec.bytes < SIZE_MIB * 1024 * 1024, "the body was cut short");
  } finally {
    await fe.stop();
    await upstream.close();
  }
});
