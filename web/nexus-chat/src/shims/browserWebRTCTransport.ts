/**
 * 替换上游 `@/lib/browserWebRTCTransport` 的类型引用。
 * vendored 的 ChatContent / TextInput 只用它的 `RealtimeConnectionState` 类型。
 */
export type RealtimeConnectionState = "idle" | "connecting" | "active" | "error";
