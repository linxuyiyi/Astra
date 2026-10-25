import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import opencode from './upstream/packages/app/vite.js'

const upstream = JSON.parse(readFileSync(new URL('./UPSTREAM.json', import.meta.url), 'utf8')) as { tag: string }
const version = upstream.tag.replace(/^v/, '')
const base = `/opencode-workspace/${version}/`

export default defineConfig({
  base,
  define: { __OPENCODE_WORKSPACE_VERSION__: JSON.stringify(version) },
  plugins: [opencode],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: {
    target: 'esnext',
    outDir: `../../services/nexus-api/web/public/opencode-workspace/${version}`,
    emptyOutDir: true,
  },
})
