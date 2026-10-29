// FE-DATA-01, FE-BP-02.AC5, FE-PPL-01.AC3, SPL-DEMO-01, SPL-DEMO-02, SPL-DEMO-04 (TECH-SPECS §3.1, §3.5, §5.4): the production composition.
// http serves people.me, projects, createProject.create, the Ready gate (missionControl), sources and knowledge; the `unavailable` adapter
// answers empty for everything else. Static checks: no production module imports a fixture or the mock adapter, http code imports no
// fixture and no NEXT_PUBLIC_ variable, and no screen imports the http adapter. No server.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { composeProductionPorts } from "@/lib/data/compose";
import type { HttpPorts } from "@/lib/data/http";
import { unavailable } from "@/lib/data/unavailable";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { isDataError } from "@/lib/domain/errors";
import type { Person } from "@/lib/domain/people";
import type { Project } from "@/lib/domain/projects";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const UUID = "3f1c2b9e-7d4a-4c1e-9b2f-0a1b2c3d4e5f";

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else out.push(path.relative(root, full).split(path.sep).join("/"));
  }
  return out;
}

const never = (): never => {
  throw new Error("must not be called");
};
const me: Person = { id: "u1", name: "Dana Example", email: "dana@example.test", kind: "person" };
const project: Project = { id: UUID, name: "P", createdAt: "", status: "unknown" };
const http: HttpPorts = {
  people: { me: () => Promise.resolve(me) },
  projects: { list: () => Promise.resolve([project]), get: () => Promise.resolve(project), subscribe: () => () => undefined },
  createProject: { create: (i) => Promise.resolve({ ...project, name: i.name }) },
  missionControl: { snapshot: never, subscribe: never, start: never, retry: never, dismiss: never, leave: never },
  sources: {
    githubEnabled: false,
    pdfOnly: true,
    add: never,
    remove: never,
    files: never,
    syncNow: never,
    addToken: never,
    upload: never,
    driveIdentity: never,
    testDrive: never,
    saveDrive: never,
  },
  knowledge: { buildAvailable: false, status: never, subscribe: never, build: never, leave: never },
};
const ports = composeProductionPorts(http);

const rejectsWith = (p: Promise<unknown>, code: string) => assert.rejects(p, (e) => isDataError(e) && e.code === code, `rejects ${code}`);

test("FE-DATA-01 (TECH-SPECS §3.1): http serves people.me, projects, createProject.create, missionControl, sources, knowledge", async () => {
  assert.equal((await ports.people.me()).name, "Dana Example");
  assert.equal(ports.projects, http.projects);
  assert.equal(ports.missionControl, http.missionControl);
  assert.equal(ports.sources, http.sources);
  assert.equal(ports.knowledge, http.knowledge);
  assert.equal((await ports.createProject.create({ name: "X", contributorIds: [] })).id, UUID);
  assert.deepEqual(Object.keys(ports.createProject), ["create"]);
});

test("SPL-DEMO-02: decisions answer empty; get is not_found; submit rejects", async () => {
  assert.deepEqual(await ports.decisions.list(UUID), []);
  assert.deepEqual(await ports.decisions.domains(UUID), []);
  await rejectsWith(ports.decisions.get(UUID, "d1"), "not_found");
  await rejectsWith(ports.decisions.submit(UUID, { decisionId: "d1", answers: {} }), "not_found");
});

test("SPL-DEMO-02: connectors list is empty and the only category is All", async () => {
  assert.deepEqual(await ports.connectors.list(), []);
  assert.deepEqual(await ports.connectors.categories(), ["All"]);
});

test("SPL-DEMO-02: plan answers an empty table and idle; create is not_available; develop is not_found; subscribe and leave do nothing", async () => {
  assert.deepEqual(await ports.plan.table(UUID), { rows: [], distinct: { critical: 0, high: 0, medium: 0, low: 0 }, unlinked: 0 });
  assert.deepEqual(await ports.plan.state(UUID), { phase: "idle" });
  await rejectsWith(ports.plan.create(UUID), "not_available");
  await rejectsWith(ports.plan.develop(UUID), "not_found");
  await rejectsWith(ports.plan.updateTaskBody(UUID, "t", { start: 0, end: 1 }, "x", 1), "not_found");
  const off = ports.plan.subscribe(UUID, never);
  off();
  ports.plan.leave(UUID);
});

test("SPL-DEMO-02: the table answer is a fresh object each time (a caller cannot change the next answer)", async () => {
  const a = await ports.plan.table(UUID);
  a.distinct.critical = 9;
  assert.equal((await ports.plan.table(UUID)).distinct.critical, 0);
});

test("SPL-DEMO-02: SpecBot greets the /me first name, offers no suggestions; ask gives one answer and done", async () => {
  const intro = await ports.assistant.intro(UUID);
  assert.match(intro.greeting, /\bDana\b/);
  assert.doesNotMatch(intro.greeting, /Example/);
  assert.deepEqual(intro.suggestions, []);
  assert.match(intro.subline, /not available yet/);
  const events = [];
  for await (const e of ports.assistant.ask(UUID, "Hello?", new AbortController().signal)) events.push(e);
  assert.deepEqual(
    events.map((e) => e.type),
    ["answer", "done"],
  );
});

test("SPL-DEMO-02: SpecBot greeting has no name when /me fails", async () => {
  const failing = composeProductionPorts({ ...http, people: { me: () => Promise.reject(new Error("down")) } });
  assert.equal((await failing.assistant.intro(UUID)).greeting, "Hi");
});

test("SPL-DEMO-02: files cannot load a tree (the screen says so), documents are not_found, upload is not_available, comments are empty", async () => {
  await rejectsWith(ports.files.tree(UUID), "not_available");
  await rejectsWith(ports.files.get(UUID, "a.md"), "not_found");
  await rejectsWith(ports.files.readme(UUID), "not_found");
  await rejectsWith(ports.files.addFiles(UUID, []), "not_available");
  await rejectsWith(ports.files.updateBlock(UUID, "a.md", { start: 0, end: 1 }, "x", 1), "not_found");
  ports.files.subscribe(UUID, never)();
  assert.deepEqual(await ports.comments.list(UUID, "a.md"), []);
  await rejectsWith(ports.comments.create(UUID, { path: "a.md", section: "s", quote: "q" }, "x"), "not_found");
  ports.comments.subscribe(UUID, never)();
});

test("SPL-DEMO-02: diagrams are not_found; the people directory and mentions are empty", async () => {
  await rejectsWith(ports.diagrams.business(UUID), "not_found");
  await rejectsWith(ports.diagrams.architecture(UUID), "not_found");
  assert.deepEqual(await ports.people.directory(), []);
  assert.deepEqual(await ports.people.mentionables(UUID), []);
});

test("SPL-DEMO-01: the composition has exactly the DataPorts members, and the unavailable adapter covers the other seven", () => {
  assert.deepEqual(
    Object.keys(unavailable).toSorted((a, b) => a.localeCompare(b)),
    ["assistant", "comments", "connectors", "decisions", "diagrams", "files", "plan"],
  );
  assert.deepEqual(
    Object.keys(ports).toSorted((a, b) => a.localeCompare(b)),
    [
      "assistant",
      "comments",
      "connectors",
      "createProject",
      "decisions",
      "diagrams",
      "files",
      "knowledge",
      "missionControl",
      "people",
      "plan",
      "projects",
      "sources",
    ],
  );
});

test("SPL-DEMO-01: the UI store keeps only the current project id and the SpecBot prefill; the handoff is taken once", () => {
  const ui = createUiStore();
  assert.deepEqual(ui.get(), { currentProjectId: null, specbotPrefill: null });
  let n = 0;
  const off = ui.subscribe(() => n++);
  ui.update((s) => ({ ...s, currentProjectId: "p1" }));
  assert.equal(ui.get().currentProjectId, "p1");
  const handoff = createUiHandoff(ui);
  handoff.setSpecBotPrefill("p1", "selected text");
  assert.equal(handoff.takeSpecBotPrefill("p2"), null);
  assert.equal(handoff.takeSpecBotPrefill("p1"), "selected text");
  assert.equal(handoff.takeSpecBotPrefill("p1"), null);
  off();
  assert.equal(n, 3);
  assert.deepEqual(
    Object.keys(ui.get()).toSorted((a, b) => a.localeCompare(b)),
    ["currentProjectId", "specbotPrefill"],
  );
});

test("SPL-DEMO-04: no app module imports a fixture or a mock adapter; the provider holds no fixture", () => {
  const files = [...walk(path.join(root, "app")), ...walk(path.join(root, "components")), ...walk(path.join(root, "lib"))].filter((f) =>
    /\.(?:ts|tsx|mjs)$/.test(f),
  );
  const bad = files.filter((rel) =>
    /from\s+["'](?:@\/mocks|@\/lib\/data\/(?:mock|store|seed\.server)|[./]+\/(?:mock|mocks|store|seed\.server))(?:\/[^"']*)?["']/.test(
      readFileSync(path.join(root, rel), "utf8"),
    ),
  );
  assert.deepEqual(bad, []);
  for (const gone of ["mocks", "lib/data/mock", "lib/data/store.ts", "lib/data/seed.server.ts", path.join("app", "demo")])
    assert.equal(existsSync(path.join(root, gone)), false, `${gone} is deleted`);
  const provider = readFileSync(path.join(root, "lib/data/provider.tsx"), "utf8");
  assert.match(provider, /composeProductionPorts\(createHttpAdapter\(\)\)/);
  assert.doesNotMatch(provider, /mock|Seed|seed/);
});

test("R7, FE-DATA-01.AC1, R12: http code imports no fixture and no NEXT_PUBLIC_ variable; screens never import the http adapter", () => {
  const httpFiles = walk(path.join(root, "lib/data/http")).concat(["lib/data/compose.ts", "lib/data/unavailable.ts"]);
  for (const rel of httpFiles) {
    const src = readFileSync(path.join(root, rel), "utf8");
    assert.doesNotMatch(src, /from\s+["'](@\/mocks|.*\/mocks)(\/[^"']*)?["']/, `${rel} imports no fixture`);
    assert.doesNotMatch(src, /NEXT_PUBLIC_|process\.env/, `${rel} reads no env`);
    assert.doesNotMatch(src, /from\s+["']@\/components/, `${rel} imports no component`);
    assert.doesNotMatch(src, /from\s+["'][^"']*mock[^"']*["']/, `${rel} imports nothing from the mock folder`);
  }
  const screens = [...walk(path.join(root, "components")), ...walk(path.join(root, "app"))].filter((f) => /\.(?:ts|tsx)$/.test(f));
  for (const rel of screens) {
    const src = readFileSync(path.join(root, rel), "utf8");
    assert.doesNotMatch(src, /lib\/data\/(http|compose|unavailable)/, `${rel} does not import the adapters`);
  }
});
