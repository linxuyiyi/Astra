#!/usr/bin/env node
import { existsSync, readFileSync } from "node:fs"
import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"
import path from "node:path"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const workspace = path.join(root, "web", "workspace-ui")
const action = process.argv[2]
if (!["install", "build", "type-check"].includes(action)) {
  console.error("Usage: node tools/e2e/workspace_ui.mjs <install|build|type-check>")
  process.exit(2)
}

const upstreamTag = JSON.parse(
  readFileSync(path.join(workspace, "UPSTREAM.json"), "utf8"),
).tag
const version = String(upstreamTag).replace(/^v/, "")
const bundle = path.join(
  root,
  "services",
  "nexus-api",
  "web",
  "public",
  "opencode-workspace",
  version,
  "index.html",
)
const dependenciesInstalled = existsSync(path.join(workspace, "node_modules"))

if (action === "build" && !dependenciesInstalled) {
  if (existsSync(bundle)) {
    console.log(
      `[workspace-ui] workspace-ui dependencies are not installed; reusing committed production bundle at services/nexus-api/web/public/opencode-workspace/${version}`,
    )
    process.exit(0)
  }
  console.error(
    `[workspace-ui] dependencies missing and no committed bundle for OpenCode ${version} at services/nexus-api/web/public/opencode-workspace/${version}/index.html; run: node tools/e2e/workspace_ui.mjs install`,
  )
  process.exit(1)
}

const windows = process.platform === "win32"

const managers = {
  pnpm: windows
    ? [
        { command: "pnpm.cmd", prefix: [] },
        { command: "corepack.cmd", prefix: ["pnpm"] },
      ]
    : [
        { command: "pnpm", prefix: [] },
        { command: "corepack", prefix: ["pnpm"] },
      ],
  bun: windows
    ? [
        { command: "bun.exe", prefix: [] },
        { command: "bun.cmd", prefix: [] },
      ]
    : [{ command: "bun", prefix: [] }],
}

const hasBunLock = existsSync(path.join(workspace, "bun.lock"))
const hasPnpmLock = existsSync(path.join(workspace, "pnpm-lock.yaml"))
const preferred = hasBunLock && !hasPnpmLock ? "bun" : "pnpm"
const order = preferred === "bun" ? ["bun", "pnpm"] : ["pnpm", "bun"]
const candidates = order.flatMap((name) => managers[name])

function run(candidate, args, options = {}) {
  return spawnSync(candidate.command, [...candidate.prefix, ...args], {
    cwd: workspace,
    stdio: options.stdio ?? "inherit",
    shell: windows,
    env: process.env,
  })
}

let manager = null
for (const candidate of candidates) {
  const probe = run(candidate, ["--version"], { stdio: "ignore" })
  if (probe.status === 0) {
    manager = candidate
    break
  }
}
if (!manager) {
  console.error(
    `workspace-ui requires ${preferred} to match its lockfile, but it is not available; install ${preferred} (fallback: pnpm or bun)`,
  )
  process.exit(1)
}

const isBun = manager.command.startsWith("bun")
if (preferred === "bun" && !isBun) {
  console.error(
    "[workspace-ui] bun.lock detected but bun is not available; falling back to pnpm. Install bun to match the committed lockfile.",
  )
} else if (preferred === "pnpm" && isBun) {
  console.error(
    "[workspace-ui] no bun.lock detected and pnpm/corepack is not available; falling back to bun.",
  )
}

const args = action === "install" ? ["install"] : ["run", action]

console.log(`[workspace-ui] ${manager.command} ${[...manager.prefix, ...args].join(" ")}`)
const result = run(manager, args)
process.exit(result.status ?? 1)
