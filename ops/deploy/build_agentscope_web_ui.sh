#!/usr/bin/env bash
# Rebuild the official AgentScope Web UI for the Nexus /agentscope-ui/ path.
#
# The script clones the pinned official tag, verifies the exact commit, installs
# from the frozen pnpm lock, applies only the three deployment patches, and
# writes the build + upstream metadata. It never edits the reference snapshot
# and does not fork AgentScope.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="$ROOT/services/nexus-api/web/public/agentscope-ui"
META="$ROOT/web/agentscope-ui"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/agentscope-web-ui.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

REPO="https://github.com/agentscope-ai/agentscope"
TAG="v2.0.10"
COMMIT="e6c014f0be49b2a6f6f62a9453082a0b975484e1"

echo "[build] cloning $REPO $TAG"
git clone --depth 1 --branch "$TAG" "$REPO" "$TMP/agentscope"
ACTUAL="$(git -C "$TMP/agentscope" rev-parse HEAD)"
if [ "$ACTUAL" != "$COMMIT" ]; then
  echo "[build] commit mismatch: expected $COMMIT, got $ACTUAL" >&2
  exit 1
fi

WEB_UI="$TMP/agentscope/examples/web_ui"
FRONTEND="$WEB_UI/frontend"
echo "[build] pnpm install --frozen-lockfile"
(cd "$WEB_UI" && pnpm install --frozen-lockfile)

python3 - "$FRONTEND" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
app = root / "src" / "App.tsx"
text = app.read_text()
old = "\t{ path: '/setup', element: <SetupPageRoute />, errorElement: <RouteError /> },\n]);"
new = "\t{ path: '/setup', element: <SetupPageRoute />, errorElement: <RouteError /> },\n], { basename: '/agentscope-ui' });"
if old not in text:
    raise SystemExit("App.tsx router anchor not found; official source changed")
app.write_text(text.replace(old, new))

client = root / "src" / "api" / "client.ts"
text = client.read_text()
old = "export const getBaseUrl = () => localStorage.getItem('server_url') ?? '';\nexport const getUserId = () => localStorage.getItem('username') ?? '';"
new = """export const getBaseUrl = () => localStorage.getItem('server_url') ?? '';
export const getUserId = () => localStorage.getItem('username') ?? '';

/** Resolve an AgentScope API path against the deployed server_url.
 * server_url may be an absolute origin or a same-origin path prefix such as
 * /agentscope-api/; leading slashes on paths are stripped so both forms keep
 * the prefix. */
export function apiUrl(path: string, baseUrl?: string): URL {
\tconst rawBase = baseUrl ?? getBaseUrl();
\tconst resolvedBase = new URL(
\t\trawBase.endsWith('/') ? rawBase : `${rawBase}/`,
\t\twindow.location.origin,
\t);
\treturn new URL(path.replace(/^[/]/, ''), resolvedBase);
}"""
if old not in text:
    raise SystemExit("client.ts base anchor not found; official source changed")
text = text.replace(old, new)
old = "\tconst url = new URL(path, baseUrl ?? getBaseUrl());"
new = "\tconst url = apiUrl(path, baseUrl);"
if old not in text:
    raise SystemExit("client.ts URL anchor not found; official source changed")
client.write_text(text.replace(old, new))

workspace = root / "src" / "api" / "workspace.ts"
text = workspace.read_text()
old = "import { ApiError, client, getBaseUrl, getUserId } from './client';"
new = "import { ApiError, apiUrl, client, getUserId } from './client';"
if old not in text:
    raise SystemExit("workspace.ts import anchor not found; official source changed")
text = text.replace(old, new)
old = "\t\tconst url = new URL('/workspace/skill/upload', getBaseUrl());"
new = "\t\tconst url = apiUrl('/workspace/skill/upload');"
if old not in text:
    raise SystemExit("workspace.ts upload URL anchor not found; official source changed")
workspace.write_text(text.replace(old, new))

knowledge = root / "src" / "api" / "knowledgeBase.ts"
text = knowledge.read_text()
old = "import { ApiError, client, getBaseUrl, getUserId } from './client';"
new = "import { ApiError, apiUrl, client, getUserId } from './client';"
if old not in text:
    raise SystemExit("knowledgeBase.ts import anchor not found; official source changed")
text = text.replace(old, new)
old = "\t\tconst url = new URL(`/knowledge_bases/${knowledgeBaseId}/documents`, getBaseUrl());"
new = "\t\tconst url = apiUrl(`/knowledge_bases/${knowledgeBaseId}/documents`);"
if old not in text:
    raise SystemExit("knowledgeBase.ts upload URL anchor not found; official source changed")
text = text.replace(old, new)
old = """\t\tconst url = new URL(
\t\t\t`/knowledge_bases/${knowledgeBaseId}/documents/${documentId}`,
\t\t\tgetBaseUrl(),
\t\t);"""
new = """\t\tconst url = apiUrl(
\t\t\t`/knowledge_bases/${knowledgeBaseId}/documents/${documentId}`,
\t\t);"""
if old not in text:
    raise SystemExit("knowledgeBase.ts download URL anchor not found; official source changed")
knowledge.write_text(text.replace(old, new))

index = root / "index.html"
text = index.read_text()
seed = """\t\t<script>
\t\t\t// Nexus 同源部署默认值；操作员已设置的值不覆盖。
\t\t\t;(function () {
\t\t\t\ttry {
\t\t\t\t\tif (!localStorage.getItem('server_url')) {
\t\t\t\t\t\tlocalStorage.setItem('server_url', '/agentscope-api/');
\t\t\t\t\t}
\t\t\t\t\tif (!localStorage.getItem('username')) {
\t\t\t\t\t\tlocalStorage.setItem('username', 'nexus');
\t\t\t\t\t}
\t\t\t\t} catch (e) {}
\t\t\t})();
\t\t</script>
"""
if "localStorage.setItem('server_url', '/agentscope-api/')" not in text:
    text = text.replace('\t\t<meta charset="UTF-8" />\n', '\t\t<meta charset="UTF-8" />\n' + seed)
text = text.replace('href="/agentscope.svg"', 'href="%BASE_URL%agentscope.svg"')
index.write_text(text)
PY

echo "[build] building official UI with /agentscope-ui/ base"
(cd "$FRONTEND" && pnpm exec vite build --base=/agentscope-ui/ --outDir dist-nexus)

if [ ! -f "$FRONTEND/dist-nexus/index.html" ]; then
  echo "[build] build did not produce index.html" >&2
  exit 1
fi

echo "[build] installing into $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$FRONTEND/dist-nexus/." "$DEST/"

echo "[build] writing upstream metadata"
mkdir -p "$META"
cp "$TMP/agentscope/LICENSE" "$META/LICENSE"
cat > "$META/UPSTREAM.json" <<JSON
{
  "repository": "$REPO",
  "tag": "$TAG",
  "commit": "$COMMIT",
  "source": "examples/web_ui/frontend",
  "build": "pnpm install --frozen-lockfile && pnpm exec vite build --base=/agentscope-ui/ --outDir dist-nexus",
  "patches": [
    "src/App.tsx: createBrowserRouter basename /agentscope-ui",
    "src/api/client.ts: apiUrl helper for path-prefixed server_url",
    "src/api/workspace.ts: upload XHR uses apiUrl",
    "src/api/knowledgeBase.ts: upload/download URL uses apiUrl",
    "index.html: same-origin server_url/username seed and %BASE_URL% icon"
  ]
}
JSON
echo "[build] done: $(du -sh "$DEST" | cut -f1)"
