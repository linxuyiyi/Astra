/** 聊天和附件共用 Nexus 同源的临时 UID。 */
export function nexusFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const uid = localStorage.getItem("nexus_owner_id");
  // 中文 UID 经父页面已设置的百分号编码 cookie 传递，不能写入 HTTP ByteString。
  if (uid && /^[\x20-\x7e]+$/.test(uid)) headers.set("X-Owner-Id", uid);
  return fetch(path, { ...init, headers, credentials: "same-origin" });
}

export class ConversationHttpError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function checked(path: string, init: RequestInit) {
  const response = await nexusFetch(path, init);
  if (response.ok) return response;
  const payload = await response.json().catch(() => null);
  throw new ConversationHttpError(
    response.status,
    typeof payload?.detail === "string" ? payload.detail : response.statusText || "请求失败",
  );
}

export async function nexusGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await checked(path, { signal })).json();
}

export async function nexusPost<T>(path: string, body: unknown, signal: AbortSignal): Promise<T> {
  return (
    await checked(path, {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  ).json();
}

export async function* readFrames<T>(
  path: string,
  signal: AbortSignal,
): AsyncGenerator<{ cursor: string; data: T }> {
  const response = await checked(path, { signal, headers: { Accept: "text/event-stream" } });
  if (!response.body) throw new Error("实时连接没有返回内容");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      // CRLF 可能分布在两个网络数据块中。
      let end;
      while ((end = /\r?\n\r?\n/.exec(buffer))) {
        const lines = buffer.slice(0, end.index).split(/\r?\n/);
        buffer = buffer.slice(end.index + end[0].length);
        const data = lines
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data)
          yield {
            cursor:
              lines
                .find((line) => line.startsWith("id:"))
                ?.slice(3)
                .trim() || "",
            data: JSON.parse(data) as T,
          };
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
