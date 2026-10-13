#!/usr/bin/env node
// Guards openspec/config.yaml against the two failure modes that are invisible
// in review:
//
//   1. a top-level key the bundled CLI never reads (it is dropped silently), and
//   2. a `rules` key that is not a valid artifact ID of the configured schema
//      (the CLI only prints a warning and ignores it).
//
// Both produce a config that looks correct in the diff but constrains nothing.
// The check drives the real CLI rather than re-implementing its parser, so it
// tracks whatever bundle is committed in .opencode/tools/openspec.mjs.

import { spawnSync } from "node:child_process"
import { readFileSync, rmSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const cli = path.join(root, ".opencode", "tools", "openspec.mjs")

// Mirrors the bundled CLI's ProjectConfigSchema.
const SUPPORTED_KEYS = ["schema", "context", "rules"]

const run = (args) => {
  const result = spawnSync(process.execPath, [cli, ...args], {
    cwd: root,
    encoding: "utf8",
  })
  if (result.status !== 0) {
    process.stderr.write(result.stderr || "")
    throw new Error(`openspec CLI failed: ${args.join(" ")}`)
  }
  return result.stdout
}

// The probe change is created and removed by this script. A fixed name could
// collide with — and then delete — a real change a user created, so the name is
// unique per run (pid + clock). Cleanup runs in `finally`, which guarantees the
// probe directory, and only the probe directory, is removed even when a check
// throws partway through.
const probeName = `config-check-probe-${process.pid}-${Date.now()}`
const probeDir = path.join(root, "openspec", "changes", probeName)
const cleanup = () => rmSync(probeDir, { recursive: true, force: true })

try {
  run(["new", "change", probeName])

  const status = JSON.parse(run(["status", "--change", probeName, "--json"]))
  const schemaName = status.schemaName

  // The instructions payload is how the CLI surfaces a parsed `context`, so a
  // missing field here means the whole file failed to load.
  const template = JSON.parse(
    run(["instructions", "proposal", "--change", probeName, "--json"]),
  )
  if (template.context === undefined || template.context === null) {
    throw new Error("openspec/config.yaml: CLI did not pick up a `context` field")
  }

  const raw = readFileSync(path.join(root, "openspec", "config.yaml"), "utf8")
  const topLevelKeys = raw
    .split("\n")
    .filter((line) => /^[A-Za-z_][\w-]*\s*:/.test(line))
    .map((line) => line.match(/^([A-Za-z_][\w-]*)/)[1])
  const unknown = topLevelKeys.filter((key) => !SUPPORTED_KEYS.includes(key))
  if (unknown.length) {
    throw new Error(
      `openspec/config.yaml: unsupported top-level key(s) the bundled CLI ignores: ${unknown.join(", ")}`,
    )
  }

  // Artifact ID audit: an unknown `rules` key only produces a CLI warning.
  const ruleKeys = raw
    .split("\n")
    .map((line) => line.match(/^ {2}([A-Za-z_][\w-]*)\s*:\s*$/))
    .filter(Boolean)
    .map((match) => match[1])
  const validIds = new Set(status.artifacts.map((artifact) => artifact.id))
  const badIds = ruleKeys.filter((key) => !validIds.has(key))
  if (badIds.length) {
    throw new Error(
      `openspec/config.yaml: rules key(s) not an artifact of schema "${schemaName}": ` +
        `${badIds.join(", ")} (valid: ${[...validIds].sort().join(", ")})`,
    )
  }

  for (const artifactId of validIds) {
    const instructions = JSON.parse(
      run(["instructions", artifactId, "--change", probeName, "--json"]),
    )
    const rules = instructions.rules || []
    for (const rule of rules) {
      if (typeof rule !== "string" || rule.trim() === "") {
        throw new Error(`openspec/config.yaml: rules.${artifactId} contains a non-string or empty rule`)
      }
    }
  }

  console.log(`ok: openspec/config.yaml applies to schema "${schemaName}" (${ruleKeys.length} rule groups)`)
} catch (err) {
  console.error(err.message)
  process.exitCode = 1
} finally {
  cleanup()
}
