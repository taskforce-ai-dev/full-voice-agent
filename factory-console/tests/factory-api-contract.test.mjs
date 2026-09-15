import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../components/factory-api.ts", import.meta.url), "utf8");
const consoleSource = await readFile(new URL("../components/FactoryConsole.tsx", import.meta.url), "utf8");
const nextConfig = await readFile(new URL("../next.config.js", import.meta.url), "utf8");

test("standalone console is a Next client component", () => {
  assert.match(consoleSource, /^"use client";/);
});

test("standalone console emits static files for the loopback-only Nginx origin", () => {
  assert.match(nextConfig, /output:\s*"export"/);
});

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
