import { onBeforeUnmount, ref, shallowRef, type Ref } from "vue";

export type AsrRecorderState = "idle" | "recording" | "transcribing" | "error";
export type AsrRecorderSource = "button" | "hotkey" | null;

const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
const ASR_ENDPOINT = "/api/asr/transcribe";
const MAX_SECONDS_DEFAULT = 60;

function mapMicrophoneError(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "麦克风权限被拒绝，请在浏览器设置中允许后重试";
  }
  if (name === "NotFoundError") return "没有找到麦克风设备";
  if (name === "NotSupportedError") return "当前浏览器不支持录音";
  if (error instanceof Error && error.message) return error.message;
  return "当前浏览器不支持录音";
}

function localizedError(code: string): string {
  const map: Record<string, string> = {
    rate_limited: "请求太频繁，请稍后再试",
    server_busy: "服务繁忙，请稍后重试",
    audio_too_long: "录音超过 60 秒",
    unsupported_audio: "音频格式无法识别",
    empty_audio: "没有识别到语音",
    recognition_timeout: "识别超时，请重试",
    payload_too_large: "录音文件超过大小限制",
    model_not_ready: "语音服务尚未就绪",
  };
  return map[code] || "";
}

function formatAsrHttpError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown; error?: unknown } | null)?.detail
    ?? (body as { error?: unknown } | null)?.error;
  const code = typeof detail === "object" && detail ? String((detail as { code?: string }).code || "") : "";
  const message = typeof detail === "string"
    ? detail
    : typeof detail === "object" && detail
      ? String((detail as { message?: string }).message || "")
      : "";
  // 网关未配置语音路由时后端返回 422 + 明确文案，必须透出，禁止静默失败。
  if (message) return message;
  const mapped = localizedError(code);
  if (mapped) return mapped;
  return status ? `识别失败（HTTP ${status}）` : "识别失败";
}

export function formatClock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(safe / 60)).padStart(2, "0");
  const ss = String(safe % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

/**
 * 浏览器录音 → POST /api/asr/transcribe → 文本。
 * 逻辑对齐 chat-island useAsrRecorder，供 native ChatPanel 复用同一 ASR API。
 */
export function useAsrRecorder(options: {
  enabled?: Ref<boolean> | boolean;
  maxSeconds?: number;
  sessionKey?: Ref<string> | string;
  onText: (text: string) => void;
}) {
  const state = ref<AsrRecorderState>("idle");
  const seconds = ref(0);
  const error = ref<string | null>(null);
  const source = ref<AsrRecorderSource>(null);
  const requesting = ref(false);

  const stateRef = { current: "idle" as AsrRecorderState };
  const streamRef = shallowRef<MediaStream | null>(null);
  const recorderRef = shallowRef<MediaRecorder | null>(null);
  const chunksRef: { current: Blob[] } = { current: [] };
  const timerRef: { current: number | null } = { current: null };
  const autoStopRef: { current: number | null } = { current: null };
  const startedAtRef = { current: 0 };
  const lastBlobRef = shallowRef<Blob | null>(null);
  const startingRef = { current: false };
  const startTokenRef = { current: 0 };
  const transcribeTokenRef = { current: 0 };
  const transcribeAbortRef: { current: AbortController | null } = { current: null };
  const discardRef = { current: false };
  const recordingSessionRef = { current: "" };
  const mountedRef = { current: true };
  const maxSeconds = options.maxSeconds ?? MAX_SECONDS_DEFAULT;

  const readEnabled = () => {
    const value = options.enabled;
    return typeof value === "boolean" ? value : value?.value !== false;
  };
  const readSessionKey = () => {
    const value = options.sessionKey;
    return typeof value === "string" ? value : value?.value ?? "";
  };

  const setPhase = (next: AsrRecorderState) => {
    stateRef.current = next;
    state.value = next;
  };

  const clearTimers = () => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    if (autoStopRef.current !== null) window.clearTimeout(autoStopRef.current);
    timerRef.current = null;
    autoStopRef.current = null;
  };

  const releaseMic = () => {
    if (streamRef.value) {
      streamRef.value.getTracks().forEach((track) => track.stop());
      streamRef.value = null;
    }
  };

  const cleanup = () => {
    clearTimers();
    releaseMic();
    recorderRef.value = null;
    chunksRef.current = [];
  };

  async function transcribe(blob: Blob, sessionAtStart: string) {
    const token = ++transcribeTokenRef.current;
    transcribeAbortRef.current?.abort();
    const controller = new AbortController();
    transcribeAbortRef.current = controller;
    error.value = null;
    setPhase("transcribing");
    source.value = null;
    const form = new FormData();
    const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
    form.append("file", blob, `recording.${ext}`);
    const timeout = window.setTimeout(() => controller.abort(), 60_000);
    try {
      const response = await fetch(ASR_ENDPOINT, { method: "POST", body: form, signal: controller.signal });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(formatAsrHttpError(response.status, body));
      const text = String((body as { text?: string }).text || "").trim();
      if (!text) throw new Error("没有识别到语音");
      if (!mountedRef.current || token !== transcribeTokenRef.current) return;
      if (sessionAtStart !== readSessionKey()) {
        setPhase("idle");
        return;
      }
      options.onText(text);
      setPhase("idle");
    } catch (err) {
      if (!mountedRef.current || token !== transcribeTokenRef.current) return;
      const message =
        err instanceof DOMException && err.name === "AbortError"
          ? "识别超时，请重试"
          : err instanceof Error
            ? err.message
            : "识别失败";
      error.value = message;
      setPhase("error");
    } finally {
      window.clearTimeout(timeout);
      if (token === transcribeTokenRef.current) transcribeAbortRef.current = null;
    }
  }

  function start(nextSource: Exclude<AsrRecorderSource, null> = "button") {
    if (!readEnabled() || startingRef.current || stateRef.current === "recording" || stateRef.current === "transcribing") {
      return;
    }
    const token = ++startTokenRef.current;
    startingRef.current = true;
    discardRef.current = false;
    recordingSessionRef.current = readSessionKey();
    requesting.value = true;
    source.value = nextSource;
    error.value = null;
    lastBlobRef.value = null;

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      startingRef.current = false;
      requesting.value = false;
      source.value = null;
      error.value = "当前浏览器不支持录音";
      setPhase("error");
      return;
    }

    void (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (!mountedRef.current || token !== startTokenRef.current) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.value = stream;
        const mimeType = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find((type) =>
          MediaRecorder.isTypeSupported(type),
        );
        const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
        recorderRef.value = recorder;
        chunksRef.current = [];
        recorder.ondataavailable = (event) => {
          if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
        };
        recorder.onerror = () => {
          cleanup();
          if (!mountedRef.current) return;
          error.value = "识别失败";
          setPhase("error");
          source.value = null;
          requesting.value = false;
        };
        recorder.onstop = () => {
          const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
          chunksRef.current = [];
          releaseMic();
          recorderRef.value = null;
          if (!mountedRef.current) return;
          if (discardRef.current) {
            discardRef.current = false;
            setPhase("idle");
            source.value = null;
            requesting.value = false;
            return;
          }
          if (blob.size < 500) {
            error.value = "没有识别到语音";
            setPhase("error");
            source.value = null;
            requesting.value = false;
            return;
          }
          if (blob.size > MAX_UPLOAD_BYTES) {
            error.value = "录音文件超过大小限制";
            setPhase("error");
            source.value = null;
            requesting.value = false;
            return;
          }
          lastBlobRef.value = blob;
          void transcribe(blob, recordingSessionRef.current);
        };
        recorder.start(250);
        startingRef.current = false;
        requesting.value = false;
        startedAtRef.current = performance.now();
        seconds.value = 0;
        setPhase("recording");
        clearTimers();
        timerRef.current = window.setInterval(() => {
          seconds.value = Math.floor((performance.now() - startedAtRef.current) / 1000);
        }, 250);
        autoStopRef.current = window.setTimeout(() => {
          if (stateRef.current === "recording" && recorder.state !== "inactive") {
            clearTimers();
            setPhase("transcribing");
            source.value = null;
            releaseMic();
            try {
              recorder.stop();
            } catch {
              cleanup();
              error.value = "识别失败";
              setPhase("error");
            }
          }
        }, maxSeconds * 1000);
      } catch (err) {
        if (token !== startTokenRef.current || !mountedRef.current) return;
        startingRef.current = false;
        requesting.value = false;
        cleanup();
        source.value = null;
        error.value = mapMicrophoneError(err);
        setPhase("error");
      }
    })();
  }

  function cancel() {
    startTokenRef.current += 1;
    startingRef.current = false;
    transcribeTokenRef.current += 1;
    transcribeAbortRef.current?.abort();
    transcribeAbortRef.current = null;
    discardRef.current = true;
    const recorder = recorderRef.value;
    clearTimers();
    releaseMic();
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch {
        /* already stopping */
      }
    }
    recorderRef.value = null;
    chunksRef.current = [];
    if (mountedRef.current) {
      requesting.value = false;
      source.value = null;
      seconds.value = 0;
      error.value = null;
      setPhase("idle");
    }
  }

  function stop() {
    if (startingRef.current) {
      cancel();
      return;
    }
    const recorder = recorderRef.value;
    if (!recorder || recorder.state === "inactive") return;
    clearTimers();
    setPhase("transcribing");
    source.value = null;
    releaseMic();
    try {
      recorder.stop();
    } catch {
      cleanup();
      setPhase("error");
      error.value = "识别失败";
    }
  }

  function retry() {
    if (stateRef.current === "transcribing" || startingRef.current) return;
    if (lastBlobRef.value) void transcribe(lastBlobRef.value, readSessionKey());
    else start("button");
  }

  function toggle() {
    if (startingRef.current) {
      cancel();
      return;
    }
    if (stateRef.current === "recording") {
      stop();
      return;
    }
    if (stateRef.current === "transcribing") return;
    if (stateRef.current === "error" && lastBlobRef.value) {
      retry();
      return;
    }
    start("button");
  }

  onBeforeUnmount(() => {
    mountedRef.current = false;
    startTokenRef.current += 1;
    transcribeTokenRef.current += 1;
    transcribeAbortRef.current?.abort();
    transcribeAbortRef.current = null;
    clearTimers();
    discardRef.current = true;
    const recorder = recorderRef.value;
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch {
        /* ignore */
      }
    }
    recorderRef.value = null;
    releaseMic();
  });

  return { state, seconds, error, source, requesting, start, stop, cancel, toggle, retry };
}
