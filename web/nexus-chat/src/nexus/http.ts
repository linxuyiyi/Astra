/**
 * Nexus API 调用：鉴权口径必须和 web/nexus/src/api.ts 一致。
 *
 * 设置页 iframe 会在 localStorage 里 seed `server_url=/agentscope-api/` 和
 * `username=nexus`；聊天岛不能沿用那套身份，否则会把浏览器自称的用户当成 AgentScope 用户。
 * 这里只走 Nexus 的 /api/v1：SSO 由 nginx 注入 X-Remote-User，本地开发回退到
 * localStorage 的 nexus_owner_id（X-Owner-Id），和 Vue 侧完全相同。
 */
let resolvedUid = "";
let proxyVerified = false;

async function ownerHeaders(): Promise<Record<string, string>> {
  if (!resolvedUid && !proxyVerified) {
    try {
      const response = await fetch("/api/v1/me", { credentials: "omit" });
      if (response.ok) {
        const payload = (await response.json()) as { uid?: string };
        if (payload.uid) {
          resolvedUid = payload.uid;
          proxyVerified = true;
        }
      }
    } catch {
      // 没有 SSO 时回退到本地 UID，和 Vue 侧一致。
    }
    if (!proxyVerified) {
      const stored = localStorage.getItem("nexus_owner_id") ?? "";
      resolvedUid = stored.startsWith("browser_") ? "" : stored;
    }
  }
  return !proxyVerified && resolvedUid ? { "X-Owner-Id": resolvedUid } : {};
}

export async function nexusFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = {
    ...(await ownerHeaders()),
    ...((init.headers as Record<string, string> | undefined) ?? {}),
  };
  return fetch(path, { ...init, headers, credentials: "omit" });
}

export async function nexusGet<T>(path: string): Promise<T> {
  const response = await nexusFetch(path);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // 保留状态文本
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}
