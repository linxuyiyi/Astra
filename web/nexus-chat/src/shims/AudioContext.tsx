/**
 * 替换上游 `@/context/AudioContext`。
 *
 * 上游把语音播报状态放在 React Context 里，`useMessages` 每条 DATA_BLOCK 事件都会写进
 * 播放器。Nexus 聊天区不做语音播报，这里保留同名 API 并恒返回 null，让 vendored 组件
 * 的 `?.` 调用自然降级为无操作；同时避免把 WebRTC / Web Audio 依赖带进产物。
 */
import { createContext, useContext, type ReactNode } from "react";

export interface StreamingAudioManager {
  start(blockId: string, mediaType: string): void;
  append(blockId: string, data: string): void;
  end(blockId: string): void;
  stopAllPlayback(): void;
  disposeAll(): void;
}

export interface ReplayController {
  play(element: HTMLAudioElement): void;
  stop(): void;
}

export interface StreamingAudioState {
  status: "idle" | "streaming";
  url?: string;
  interruptCount?: number;
}

const AudioContext = createContext<StreamingAudioManager | null>(null);

export function AudioProvider({ children }: { children: ReactNode }) {
  return <AudioContext.Provider value={null}>{children}</AudioContext.Provider>;
}

export function useAudioManager(): StreamingAudioManager | null {
  return useContext(AudioContext);
}

export function useReplayController(): ReplayController | null {
  return null;
}

export function useAudioBlock(_blockId: string | undefined): StreamingAudioState | null {
  return null;
}
