import { render } from 'solid-js/web'
import { HashRouter } from '@solidjs/router'
import {
  AppBaseProviders,
  AppInterface,
  configureWorkspaceCapabilities,
  PlatformProvider,
  ServerConnection,
  type Platform,
} from '@opencode-ai/app'
import { base64Encode } from '@opencode-ai/core/util/encode'
import { streamRecovery } from './stream-recovery'

declare const __OPENCODE_WORKSPACE_VERSION__: string

// Only binding belongs here. Conversation, tools, files, diff, composer and theme are upstream;
// Astra must not restyle the OpenCode surface.
const taskID = new URLSearchParams(location.search).get('task')
const selected = new URLSearchParams(location.search).get('session')
const root = document.getElementById('root')!
const nativeFetch = window.fetch.bind(window)

const OPENCODE_ROOTS = [
  '/event',
  '/session',
  '/provider',
  '/project',
  '/config',
  '/path',
  '/file',
  '/vcs',
  '/find',
  '/lsp',
  '/agent',
  '/command',
  '/skill',
  '/mcp',
  '/permission',
  '/question',
  '/pty',
  '/tui',
  '/log',
  '/auth',
  '/experimental',
  '/formatter',
]

function opencodePath(pathname: string) {
  if (pathname.startsWith('/api/') || pathname.startsWith('/global/')) return true
  return OPENCODE_ROOTS.some((item) => pathname === item || pathname.startsWith(`${item}/`))
}

// @opencode-ai/client builds `new URL("/api/...", baseUrl)`. An absolute path drops the
// task prefix, so the iframe would call the Nexus origin instead of the Worker proxy.
function rewriteOpenCodeUrl(input: RequestInfo | URL, endpoint: string) {
  const raw = input instanceof Request ? input.url : String(input)
  let url: URL
  try {
    url = new URL(raw, location.origin)
  } catch {
    return null
  }
  if (url.origin !== location.origin) return null
  const path = url.pathname
  if (path.startsWith('/opencode-workspace/') || path.startsWith('/api/tasks/')) return null
  if (!opencodePath(path)) return null
  return endpoint + path + url.search
}

const OFFLINE_NOTICE = 'Worker 不在线。正在自动重试…'
const SOLID_CRASH = ['加载应用程序时发生错误', 'An error occurred while loading the application']
const RECOVER_KEY = 'astra-workspace-recover'

function workspaceURL() {
  const query = selected ? `?session_id=${encodeURIComponent(selected)}` : ''
  return `/api/tasks/${encodeURIComponent(taskID || '')}/workspace${query}`
}

let disposeApp: (() => void) | undefined
let offline = false

function showOfflineNotice() {
  if (offline) return
  offline = true
  disposeApp?.()
  disposeApp = undefined
  root.replaceChildren()
  const notice = document.createElement('p')
  notice.dataset.astraNotice = 'offline'
  notice.textContent = OFFLINE_NOTICE
  root.append(notice)
}

function pageCrashed() {
  const text = root.textContent || ''
  return SOLID_CRASH.some(item => text.includes(item))
}

function watchWorker(endpoint: string, sessionID = selected || '') {
  let lastBinding = ''
  const observer = new MutationObserver(() => {
    if (pageCrashed()) showOfflineNotice()
  })
  observer.observe(root, { subtree: true, childList: true, characterData: true })
  window.setInterval(async () => {
    if (offline || pageCrashed()) {
      if (pageCrashed()) showOfflineNotice()
      return
    }
    try {
      const response = await nativeFetch(workspaceURL())
      if (!response.ok) return
      const binding = await response.json()
      if (binding?.runtime?.worker_online === false) showOfflineNotice()
      else {
        const fact = JSON.stringify([binding.task_status, binding.runtime?.connection, binding.runtime?.error])
        if (lastBinding !== fact) {
          lastBinding = fact
          notifyParent(sessionID)
        }
      }
    } catch {
      // The platform itself blipped. Leave the open workspace in place.
    }
  }, 1000)
  const recover = async () => {
    if (!offline) return
    try {
      const response = await nativeFetch(workspaceURL())
      if (!response.ok) return
      const binding = await response.json()
      if (binding?.runtime?.worker_online === false) return
      if (endpoint) {
        const health = await nativeFetch(`${endpoint}/global/health`)
        if (!health.ok) return
      }
      const last = Number(sessionStorage.getItem(RECOVER_KEY) || 0)
      if (Date.now() - last < 8000) return
      sessionStorage.setItem(RECOVER_KEY, String(Date.now()))
      location.reload()
    } catch {
      // Still unreachable. The next tick retries.
    }
  }
  window.setInterval(() => { void recover() }, 1000)
}

function notifyParent(sessionID: string) {
  if (!sessionID) return
  window.parent.postMessage({ type: 'astra-workspace-ready', taskId: taskID, sessionId: sessionID }, location.origin)
}

function installFetchRewrite(endpoint: string) {
  const recoverStream = streamRecovery(showOfflineNotice)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const next = rewriteOpenCodeUrl(input, endpoint)
    const response = next
      ? await nativeFetch(input instanceof Request ? new Request(next, input) : next, init)
      : await nativeFetch(input, init)
    if (next && response.status === 503) {
      void response.clone().text().then(text => {
        if (text.includes('不在线') || text.includes('不可连接')) showOfflineNotice()
      }).catch(() => {})
    }
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined)
    return next ? recoverStream(new URL(next).pathname, response, signal) : response
  }
}

async function boot() {
  if (!taskID) throw new Error('缺少 Task ID')
  const response = await nativeFetch(workspaceURL())
  const binding = await response.json()
  if (!response.ok) throw new Error(binding.detail)
  if (binding.runtime.worker_online === false) {
    throw new Error('Worker 不在线。恢复连接后再打开这个 Session。')
  }
  if (binding.runtime.opencode_version !== __OPENCODE_WORKSPACE_VERSION__) {
    throw new Error(`Workspace UI ${__OPENCODE_WORKSPACE_VERSION__} 与 OpenCode ${binding.runtime.opencode_version} 不匹配`)
  }
  const sessionID = selected || binding.runtime.session_id
  if (!sessionID) throw new Error('任务尚未绑定 OpenCode Session')
  if (!binding.sessions.some((session: { id: string }) => session.id === sessionID)) throw new Error('Session 不属于此任务')
  const endpoint = `${location.origin}${binding.runtime.server}/${encodeURIComponent(sessionID)}`
  installFetchRewrite(endpoint)
  const server: ServerConnection.Http = { type: 'http', http: { url: endpoint } }
  location.hash = `/${base64Encode(binding.runtime.workspace)}/session/${sessionID}`
  const platform: Platform = {
    platform: 'web', version: __OPENCODE_WORKSPACE_VERSION__,
    fetch: (input, init) => window.fetch(input, init),
    openExternal: url => { window.open(url, '_blank', 'noopener,noreferrer') },
    restart: async () => location.reload(),
    notify: async () => {},
    // Isolate persisted upstream preferences and server selection per task.
    storage: name => ({
      getItem: key => localStorage.getItem(`${taskID}:${name}:${key}`),
      setItem: (key, value) => localStorage.setItem(`${taskID}:${name}:${key}`, value),
      removeItem: key => localStorage.removeItem(`${taskID}:${name}:${key}`),
    }),
  }
  // Astra owns session creation and lifecycle: the embedded workspace only shows the bound session.
  configureWorkspaceCapabilities({ sessionManagementEnabled: false })
  disposeApp = render(() => <PlatformProvider value={platform}><AppBaseProviders><AppInterface
    defaultServer={ServerConnection.Key.make(endpoint)} servers={[server]} router={HashRouter}
  ></AppInterface></AppBaseProviders></PlatformProvider>, root)
  sessionStorage.removeItem(RECOVER_KEY)
  watchWorker(endpoint, sessionID)
  notifyParent(sessionID)
}
boot().catch(error => {
  const message = String(error.message || error)
  if (message.includes('不在线')) {
    showOfflineNotice()
    watchWorker('')
    return
  }
  root.textContent = message
})
