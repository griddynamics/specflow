// MCP-M10 (FR-MCP-UI-04, 07, 08): the snippet builders. Exact strings, values from the inputs, and the binding file against the contract.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import {
  bindCommand,
  connectPrompt,
  bindingFileJson,
  CHECK_PROMPT,
  claudeAddCommand,
  DEVELOP_OVERVIEW_PROMPT,
  DEVELOP_START_PROMPT,
  SIGN_IN_COMMAND,
  teamMcpJson,
} from "../../lib/guides/snippets";

const PROD = { mcpUrl: "https://mcp.example.com/mcp", clientId: "my-client", serverName: "specflow" };
const NAMED = { ...PROD, serverName: "acme-specflow" };
const ID = "0b8b2c1e-6f3a-4c52-9d1e-7a5f4b3c2d10";

test("claude mcp add: client id and address come from the inputs", () => {
  assert.equal(
    claudeAddCommand(PROD),
    "claude mcp add --transport http --scope user --client-id my-client --callback-port 8765 specflow https://mcp.example.com/mcp",
  );
});

test("claude mcp add: the server name is the configured one", () => {
  assert.equal(
    claudeAddCommand(NAMED),
    "claude mcp add --transport http --scope user --client-id my-client --callback-port 8765 acme-specflow https://mcp.example.com/mcp",
  );
});

test("team .mcp.json: pretty, two spaces, values from the inputs", () => {
  const text = teamMcpJson(PROD);
  assert.equal(
    text,
    `{
  "mcpServers": {
    "specflow": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "clientId": "my-client",
        "callbackPort": 8765
      }
    }
  }
}`,
  );
  assert.deepEqual(JSON.parse(text), {
    mcpServers: { specflow: { type: "http", url: PROD.mcpUrl, oauth: { clientId: "my-client", callbackPort: 8765 } } },
  });
  assert.deepEqual(Object.keys((JSON.parse(teamMcpJson(NAMED)) as { mcpServers: object }).mcpServers), ["acme-specflow"]);
});

test("constants: sign-in command, check prompt, sample prompts", () => {
  assert.equal(SIGN_IN_COMMAND, "/mcp");
  assert.equal(CHECK_PROMPT, "Which SpecFlow projects can I work on?");
  assert.equal(DEVELOP_OVERVIEW_PROMPT, "List the features of this project.");
  assert.equal(DEVELOP_START_PROMPT, "Summarise each feature of this project in one line, then propose where to start in this repository.");
});

test("bind command: the configured name, with and without a project id", () => {
  assert.equal(bindCommand("specflow"), "/mcp__specflow__select_project");
  assert.equal(bindCommand("acme-specflow"), "/mcp__acme-specflow__select_project");
  assert.equal(bindCommand("specflow", ID), `/mcp__specflow__select_project ${ID}`);
  assert.equal(bindCommand("acme-specflow", ID), `/mcp__acme-specflow__select_project ${ID}`);
  assert.equal(bindCommand("specflow", ""), "/mcp__specflow__select_project");
});

test("connect prompt: with a name, without one, and with a quote in the name", () => {
  assert.equal(connectPrompt("Checkout refactor"), 'Connect this repository to the SpecFlow project "Checkout refactor".');
  assert.equal(connectPrompt(null), "Connect this repository to the SpecFlow project <project name>.");
  assert.equal(connectPrompt('The "big" one'), 'Connect this repository to the SpecFlow project "The "big" one".');
});

// The binding file against the committed contract (E3): Ajv 8, draft 2020-12, with formats asserted.
const schema = JSON.parse(
  readFileSync(path.join(import.meta.dirname, "../../../backend/contracts/schemas/specflow-workspace-config.schema.json"), "utf8"),
) as object;
const ajv = new Ajv2020({ strict: true, allErrors: true });
addFormats(ajv);
const validate = ajv.compile(schema);

test("binding file JSON validates against the contract schema", () => {
  const names = ['Checkout "refactor"', "Zażółć gęślą jaźń — 日本語", "back\\slash and 'quotes'", "x".repeat(200)];
  const servers = ["http://localhost:8002/mcp", "https://mcp.example.com/mcp"];
  for (const projectName of names) {
    for (const mcpUrl of servers) {
      const text = bindingFileJson({ mcpUrl, projectId: ID, projectName });
      const parsed = JSON.parse(text) as Record<string, unknown>;
      assert.deepEqual(parsed, { version: 1, server: mcpUrl, project_id: ID, project_name: projectName });
      assert.equal(validate(parsed), true, JSON.stringify(validate.errors));
      assert.ok(text.includes('\n  "project_id"'), "pretty, two spaces");
    }
  }
});

test("the contract rejects what the builder would produce from a bad value (the test is not vacuous)", () => {
  assert.equal(
    validate(JSON.parse(bindingFileJson({ mcpUrl: "https://mcp.example.com/mcp", projectId: "nope", projectName: "A" }))),
    false,
  );
  assert.equal(validate(JSON.parse(bindingFileJson({ mcpUrl: "file:///x", projectId: ID, projectName: "A" }))), false);
});
