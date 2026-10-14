// Pair Desk plugin for opencode.
//
// Installed by integrations/opencode/install.py as a one-line re-export in
// ~/.config/opencode/plugins/pair-desk.js, so updates to this repo apply without reinstalling.
//
// What it does, only in folders linked to a Pair Desk project (a `.pair-desk.json` marker or
// `desk.py link`), and never writing anything to the desk:
// - session start summary: the first time a session talks to the model, it runs
//   `desk.py hook session-start` for the session's folder (through bin/pair-desk, which picks
//   python3 or python; see deskCommand) (the same summary the Claude
//   Code SessionStart hook gives) and keeps the line for that session;
// - system prompt: every request of that session carries a short Pair Desk block (the rules
//   and the summary line) through `experimental.chat.system.transform`. The block is fixed per
//   session, so it does not break prompt caching;
// - toast: when a session is created in the TUI, the summary line shows as a toast.
//
// The MCP tools (`pair-desk_*`) come from the `mcp` entry the installer adds to opencode's
// config; the full workflow is in the `pair-desk` skill.

import { spawn } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const TIMEOUT_MS = 10000

const RULES = [
  "Pair Desk is the game owner's local playtest and issue desk; this folder is linked to one of its projects.",
  "Use the pair-desk MCP tools (list_projects, list_issues, get_issue, create_issue, comment, set_status,",
  "queue_command, import_checks) and load the `pair-desk` skill for the full workflow.",
  "At session start read failed checks, then new reports, before other work.",
  "After a change the owner should verify in game, file a check (create_issue) with location commands, one place each (and the world seed, if the project uses seeds).",
  "Use update_issue to clarify reports and set milestones; original owner wording is preserved.",
  "Use auto_check for screenshots, logs and tests you can verify yourself. Record evidence; move to to_check only for remaining manual owner review, or close per project rules.",
  "Never mark anything passed; only the owner gives that verdict.",
].join(" ")

/**
 * [command, args] that run desk.py with `extra` arguments on this machine:
 * - $PAIR_DESK_PYTHON set: that interpreter with desk.py;
 * - Windows: bin\pair-desk.cmd through cmd.exe (Node cannot spawn a .cmd directly), which picks
 *   py -3, python or python3, whichever is a Python 3.11+;
 * - macOS and Linux: the bin/pair-desk launcher, which picks python3 or python the same way.
 */
export function deskCommand(extra, { root = ROOT, python = process.env.PAIR_DESK_PYTHON, platform = process.platform } = {}) {
  if (python) return [python, [path.join(root, "desk.py"), ...extra]]
  if (platform === "win32") {
    const launcher = path.win32.join(root, "bin", "pair-desk.cmd")
    return [process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", `""${launcher}" ${extra.join(" ")}"`]]
  }
  return [path.join(root, "bin", "pair-desk"), extra]
}

/** Run the desk's SessionStart hook for `cwd`; resolves to {line, context} or null. Never rejects. */
export function deskSummary(cwd, { root = ROOT, python = process.env.PAIR_DESK_PYTHON, timeoutMs = TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    let out = ""
    let done = false
    const finish = (value) => {
      if (!done) {
        done = true
        resolve(value)
      }
    }
    let child
    try {
      const [command, args] = deskCommand(["hook", "session-start", "--client", "opencode"], { root, python })
      child = spawn(command, args, {
        windowsHide: true,
        windowsVerbatimArguments: process.platform === "win32" && !python,
        stdio: ["pipe", "pipe", "ignore"],
      })
    } catch {
      return finish(null)
    }
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {}
      finish(null)
    }, timeoutMs)
    child.on("error", () => {
      clearTimeout(timer)
      finish(null)
    })
    child.stdout.on("data", (chunk) => (out += chunk))
    child.on("close", () => {
      clearTimeout(timer)
      try {
        const data = JSON.parse(out.trim() || "null")
        const line = data?.systemMessage
        const context = data?.hookSpecificOutput?.additionalContext
        finish(line ? { line, context: context || line } : null)
      } catch {
        finish(null)
      }
    })
    try {
      child.stdin.end(JSON.stringify({ cwd, hook_event_name: "SessionStart", source: "opencode" }))
    } catch {}
  })
}

/** The system prompt block for a summary (or null when the folder is not linked). */
export function systemBlock(summary) {
  if (!summary) return null
  return `<pair-desk>\n${RULES}\n${summary.context}\n</pair-desk>`
}

export const PairDeskPlugin = async ({ client, directory }) => {
  const perSession = new Map() // sessionID -> Promise<summary|null>
  const sessionDirs = new Map() // sessionID -> directory from session.created

  const summaryFor = (sessionID) => {
    const key = sessionID || ""
    if (!perSession.has(key)) perSession.set(key, deskSummary(sessionDirs.get(key) || directory))
    return perSession.get(key)
  }

  return {
    event: async ({ event }) => {
      try {
        if (event?.type === "session.created") {
          const info = event.properties?.info
          if (!info?.id) return
          if (info.directory) sessionDirs.set(info.id, info.directory)
          if (info.parentID) return // subagent sessions: no toast
          const summary = await summaryFor(info.id)
          if (summary && client?.tui?.showToast) {
            await client.tui.showToast({ body: { title: "Pair Desk", message: summary.line, variant: "info" } })
          }
        } else if (event?.type === "session.deleted") {
          const id = event.properties?.info?.id
          if (id) {
            perSession.delete(id)
            sessionDirs.delete(id)
          }
        }
      } catch {
        // a missing desk or a failed toast must never disturb the session
      }
    },
    "experimental.chat.system.transform": async (input, output) => {
      try {
        const block = systemBlock(await summaryFor(input?.sessionID))
        if (block && Array.isArray(output?.system) && !output.system.includes(block)) output.system.push(block)
      } catch {}
    },
  }
}
