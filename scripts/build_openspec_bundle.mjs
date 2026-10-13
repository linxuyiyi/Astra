#!/usr/bin/env node
// Regenerates the committed, self-contained OpenSpec CLI bundle used by Nexus's
// project-level OpenSpec integration (see .opencode/tools/openspec.mjs).
//
// The bundle removes the dependency on a globally installed `openspec`. It is a
// maintenance-only script: normal developers never run it, they just use the
// committed bundle. Run it after upgrading OpenSpec:
//
//   node scripts/build_openspec_bundle.mjs
//
// OpenSpec itself is resolved from (in order): NEXUS_OPENSPEC_PACKAGE,
// frontend/node_modules, the global npm root, or a globally installed `openspec`.

import { spawnSync } from "node:child_process"
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { createRequire } from "node:module"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const output = path.join(root, ".opencode", "tools", "openspec.mjs")

const require = createRequire(import.meta.url)

function firstExisting(candidates) {
  return candidates.find((candidate) => candidate && existsSync(candidate))
}

function resolvePackageDir() {
  if (process.env.NEXUS_OPENSPEC_PACKAGE) {
    return process.env.NEXUS_OPENSPEC_PACKAGE
  }
  const local = firstExisting([
    path.join(root, "frontend", "node_modules", "@fission-ai", "openspec"),
    path.join(root, "node_modules", "@fission-ai", "openspec"),
  ])
  if (local) return local
  const npmRoot = spawnSync("npm", ["root", "-g"], { encoding: "utf8" }).stdout?.trim()
  const globalDir = npmRoot ? path.join(npmRoot, "@fission-ai", "openspec") : null
  if (globalDir && existsSync(globalDir)) return globalDir
  return null
}

function resolveEsbuild() {
  const local = firstExisting([
    path.join(root, "frontend", "node_modules", ".bin", process.platform === "win32" ? "esbuild.cmd" : "esbuild"),
    path.join(root, "node_modules", ".bin", process.platform === "win32" ? "esbuild.cmd" : "esbuild"),
  ])
  return local ?? "esbuild"
}

const packageDir = resolvePackageDir()
if (!packageDir) {
  console.error(
    "[build_openspec_bundle] OpenSpec package not found. Install it locally or globally, " +
      "or set NEXUS_OPENSPEC_PACKAGE=/path/to/@fission-ai/openspec",
  )
  process.exit(1)
}

const packageJson = JSON.parse(readFileSync(path.join(packageDir, "package.json"), "utf8"))
const version = packageJson.version
const cliEntry = path.join(packageDir, "dist", "cli", "index.js")
const crossSpawnEntry = path.join(packageDir, "node_modules", "cross-spawn", "index.js")

if (!existsSync(cliEntry) || !existsSync(crossSpawnEntry)) {
  console.error(`[build_openspec_bundle] unexpected package layout at ${packageDir}`)
  process.exit(1)
}

const esbuild = resolveEsbuild()
const temp = mkdtempSync(path.join(os.tmpdir(), "nexus-openspec-bundle."))
const entry = path.join(temp, "entry.mjs")

writeFileSync(
  entry,
  [
    `import crossSpawn from ${JSON.stringify(crossSpawnEntry)};`,
    "globalThis.__astraCrossSpawn = crossSpawn;",
    `import { runCli } from ${JSON.stringify(cliEntry)};`,
    "runCli();",
  ].join("\n"),
)

const esbuildArgs = [
  entry,
  "--bundle",
  "--platform=node",
  "--format=esm",
  "--target=node20",
  "--banner:js=import { createRequire as __cr } from 'module'; const require = __cr(import.meta.url);",
  `--outfile=${output}`,
  "--log-level=warning",
]

const result = spawnSync(esbuild, esbuildArgs, { cwd: temp, stdio: "inherit", shell: process.platform === "win32" })
rmSync(temp, { recursive: true, force: true })
if (result.status !== 0) {
  console.error("[build_openspec_bundle] esbuild failed")
  process.exit(result.status ?? 1)
}

// Two runtime `require()` calls survive esbuild (they are built via
// createRequire and cannot be statically resolved): the package.json version
// lookup and cross-spawn. Inline both so the bundle is self-contained.
let bundle = readFileSync(output, "utf8")
bundle = bundle
  .replace(/require\w*\((["'])(?:\.\.\/)+package\.json\1\)/g, `({version:${JSON.stringify(version)}}|0)`)
  .replace(/require\w*\((["'])cross-spawn\1\)/g, "globalThis.__astraCrossSpawn")
// cli/index.js auto-runs when `process.argv[1]` resolves to its own module URL.
// In a bundle that URL is always the bundle itself, which would run the CLI a
// second time on top of the entry's explicit `runCli()`. Neutralize it.
bundle = bundle.replace(
  /if \(process\.argv\[1\] &&[^\n]*import\.meta\.url\)\) \{\s*runCli\(\);\s*\}/,
  "/* auto-run disabled: entry calls runCli() exactly once */",
)
// esbuild leaves `// <relative path back to the build machine>` module banners
// and CJS wrapper keys like `"../../../<abs>/node_modules/.../x.js"`. Drop the
// banners and strip the absolute prefix so the committed bundle does not embed
// the maintainer's file layout.
bundle = bundle
  .split("\n")
  .filter((line) => !(line.startsWith("// ") && /node_modules|@fission-ai\/openspec/.test(line)))
  .join("\n")
  .replace(/"\.\.\/(?:\.\.\/)*[^"]*?(?=@fission-ai\/openspec\/)/g, '"')
if (bundle.includes("runCli();") && !bundle.includes("auto-run disabled")) {
  console.error("[build_openspec_bundle] failed to neutralize the duplicate auto-run")
  process.exit(1)
}

writeFileSync(output, bundle)
console.log(`[build_openspec_bundle] wrote ${path.relative(root, output)} (openspec ${version})`)