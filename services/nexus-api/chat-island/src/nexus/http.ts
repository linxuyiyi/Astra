/** 所有聊天岛请求复用同源 HttpOnly 登录会话。 */
export async function nexusFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(path, { ...init, credentials: "same-origin" });
}

export async function nexusGet<T>(path: string): Promise<T> {
  const response = await nexusFetch(path);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = String(((await response.json()) as { detail?: string }).detail || detail); } catch { /* HTTP 状态 */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}
