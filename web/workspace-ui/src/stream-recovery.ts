/** An interrupted SSE cannot replay events lost while disconnected. Reopen the bound UI. */
export function streamRecovery(onReconnect: () => void) {
  const interrupted = new Set<string>()
  return (url: string, response: Response, signal?: AbortSignal | null): Response => {
    if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) return response
    if (interrupted.has(url)) {
      if (signal?.aborted) return response
      interrupted.delete(url)
      onReconnect()
      return response
    }
    const reader = response.body.getReader()
    let cancelled = false
    const lost = () => { if (!cancelled && !signal?.aborted) interrupted.add(url) }
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const part = await reader.read()
          if (part.done) { lost(); controller.close() }
          else controller.enqueue(part.value)
        } catch (error) { lost(); controller.error(error) }
      },
      async cancel(reason) { cancelled = true; await reader.cancel(reason) },
    })
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers })
  }
}
