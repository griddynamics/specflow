// Counting stub upstream for AC-12.4: counts bytes and SHA-256 of whatever is POSTed,
// optionally reading slowly (throttle, MiB/s) to model an API/GCS slower than the browser.
import http from "node:http";
import crypto from "node:crypto";
import { listen, close } from "./servers.mjs";

export async function startCountingUpstream({ port, throttleMiBps = 0 }) {
  const uploads = []; // {bytes, sha256, complete, contentType, headers}
  const server = http.createServer((req, res) => {
    const hash = crypto.createHash("sha256");
    const rec = { bytes: 0, sha256: null, complete: false, aborted: false, headers: { ...req.headers }, url: req.url };
    uploads.push(rec);
    req.on("data", (chunk) => {
      rec.bytes += chunk.length;
      hash.update(chunk);
      if (throttleMiBps) {
        req.pause();
        setTimeout(() => req.resume(), (chunk.length / (throttleMiBps * 1048576)) * 1000);
      }
    });
    req.on("end", () => {
      rec.complete = true;
      rec.sha256 = hash.digest("hex");
      res.writeHead(201, { "content-type": "application/json" });
      res.end(JSON.stringify({ document: { id: "d1", status: "uploaded", size_bytes: rec.bytes } }));
    });
    req.on("aborted", () => (rec.aborted = true));
    req.on("close", () => {
      if (!rec.complete) rec.aborted = true;
    });
  });
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  await listen(server, port);
  return { url: `http://127.0.0.1:${port}`, uploads, close: () => close(server) };
}
