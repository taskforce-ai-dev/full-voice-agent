import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../components/factory-api.ts", import.meta.url), "utf8");

test("factory client uses the owner facade v1 jobs contract", () => {
  assert.match(source, /request<FactoryJob>\("\/v1\/jobs"/);
  for (const operation of ["inspect", "plan", "approve-knowledge", "approve-plan", "generate", "verify", "open-pr"]) assert.match(source, new RegExp(`"${operation}"`));
  assert.match(source, /`\/v1\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/\$\{operation\}`/);
  assert.doesNotMatch(source, /\/api\/factory\//);
});

test("review approvals are digest payloads, never client-created artifacts", () => {
  assert.match(source, /JSON\.stringify\(\{ digest \}\)/);
  assert.doesNotMatch(source, /sha256:[0-9a-f…]+/);
});
