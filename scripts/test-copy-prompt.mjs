import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(process.argv[2] || new URL("../web/app.js", import.meta.url), "utf8");
assert.ok(source.includes('data-act="copy-prompt"'), "Entry must expose Copy as prompt");
assert.ok(source.includes('case "copy-prompt": copyText(issuePrompt('), "Action must copy the formatted entry");
const context = vm.createContext({ URL, gameCommands: (loc) => loc?.commands || [] });
vm.runInContext(source.slice(source.indexOf("function issuePrompt("), source.indexOf("async function copyText(")), context);
const minimal = { id: "PD-1", title: "Small entry", status: "open", kind: "task", priority: "p2" };
const format = (i) => context.issuePrompt(i, { slug: "pairdesk", name: "Pair Desk" }, "http://localhost:8765");
assert.ok(format(minimal).includes("# PD-1: Small entry"));
assert.ok(!format(minimal).includes("undefined"));
const attachment = { filename: "shot.png", url: "/api/attachments/42" };
const entry = { ...minimal, body: "**Exact** description <tag>", tags: ["ui"],
  plan: { steps: [{ state: "done", text: "Investigate", note: "Evidence", commit: "abc123" }], verification: "Check clipboard" },
  location: { commands: [{ command: "/goto 1 2 3", label: "Start" }], seed: 0, action: "Open menu" },
  build: { label: "Build 1", path: "1.0", commit: "def456" }, attachments: [attachment],
  comments: [{ author: "owner", created_at: "2026-10-20", text: "Still broken", verdict: "failed", attachments: [attachment] }] };
const before = JSON.stringify(entry);
const prompt = format(entry);
for (const text of ["http://localhost:8765/#/pairdesk/PD-1", entry.body, "[done] Investigate", "Evidence", "abc123", "Check clipboard", "Start: /goto 1 2 3", "seed: 0", "Open menu", "def456", "Still broken", "failed", "http://localhost:8765/api/attachments/42"]) assert.ok(prompt.includes(text), text);
assert.equal(prompt.split("shot.png:").length - 1, 1, "Deduplicate comment attachments");
assert.equal(JSON.stringify(entry), before, "Copy must not mutate entry");
console.log("Copy prompt checks passed (minimal/full entries, context, attachments, action wiring, no mutation).");
