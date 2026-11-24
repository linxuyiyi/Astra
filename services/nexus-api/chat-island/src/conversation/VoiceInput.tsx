import { useRef, useState } from "react";
import { Mic, Square, Keyboard } from "lucide-react";
import { useAsrHotkey } from "../hooks/useAsrHotkey";
import { useAsrRecorder } from "../hooks/useAsrRecorder";
import {
  DEFAULT_ASR_SHORTCUT, formatShortcut, loadAsrShortcut, saveAsrShortcut,
  shortcutFromKeyboardEvent, validateAsrShortcut, type AsrShortcut,
} from "../lib/asrShortcut";
import "./voice.css";

export function VoiceInput({ disabled, sessionKey, onText }: {
  disabled: boolean; sessionKey: string; onText: (text: string) => void;
}) {
  const mic = useAsrRecorder({ enabled: !disabled, sessionKey, onText });
  const [shortcut, setShortcut] = useState(loadAsrShortcut);
  const [pendingShortcut, setPendingShortcut] = useState<AsrShortcut>();
  const [keyError, setKeyError] = useState("");
  const settingsButton = useRef<HTMLButtonElement>(null);
  const editing = pendingShortcut !== undefined;
  const recording = mic.state === "recording";
  useAsrHotkey({
    enabled: !disabled && !editing, shortcut,
    canStart: !mic.requesting && (mic.state === "idle" || mic.state === "error"),
    recording: recording || mic.requesting, onStart: () => mic.start("hotkey"),
    onStop: mic.stop, onCancel: mic.cancel,
  });

  function close() {
    setPendingShortcut(undefined);
    setKeyError("");
    settingsButton.current?.focus();
  }
  function save(next: AsrShortcut) {
    saveAsrShortcut(next);
    setShortcut(next);
    close();
  }

  const label = recording ? "停止录音" : "语音输入（转文字）";
  const status = mic.requesting ? "正在请求麦克风…" : recording ? `录音中 · ${mic.seconds} 秒` :
    mic.state === "transcribing" ? "正在识别…" : mic.error;
  return <div className="nx-voice">
    <button type="button" aria-label={label} title={`${label} · 按住 ${formatShortcut(shortcut)} 说话`}
      data-testid="asr-mic" disabled={disabled || mic.state === "transcribing"}
      onClick={mic.toggle}>{recording ? <Square size={16} /> : <Mic size={17} />}</button>
    <button type="button" aria-label="语音快捷键" title={`按住 ${formatShortcut(shortcut)} 说话`}
      data-testid="asr-shortcut-hint" ref={settingsButton} aria-expanded={editing} className="nx-voice-settings"
      onClick={() => editing ? close() : setPendingShortcut(shortcut)}>
      <Keyboard size={15} />
    </button>
    {status && <div className="nx-voice-status" data-testid="asr-status" role={mic.error ? "alert" : "status"}>
      {status}
      {mic.error && <button type="button" onClick={mic.retry}>重试</button>}
      <button type="button" onClick={mic.cancel}>{mic.error ? "关闭" : "取消"}</button>
    </div>}
    {editing && <div className="nx-voice-popover" role="dialog" aria-label="语音快捷键" data-testid="asr-shortcut-dialog">
      <label>按住说话的快捷键
        <input autoFocus readOnly value={formatShortcut(pendingShortcut)} aria-label="按下新的快捷键"
          onKeyDown={event => {
            if (event.key === "Tab") return;
            event.preventDefault();
            event.stopPropagation();
            if (event.key === "Escape") return close();
            const next = shortcutFromKeyboardEvent(event.nativeEvent);
            if (!next) return;
            setPendingShortcut(next);
            setKeyError(validateAsrShortcut(next).ok ? "" : "请选择包含 Alt、Ctrl 或 Command 的可用组合键");
          }} />
      </label>
      {keyError && <small role="alert">{keyError}</small>}
      <button type="button" data-testid="asr-shortcut-reset" onClick={() => {
        setPendingShortcut(DEFAULT_ASR_SHORTCUT);
        setKeyError("");
      }}>恢复默认</button>
      <button type="button" onClick={close}>关闭</button>
      <button type="button" data-testid="asr-shortcut-save" disabled={!validateAsrShortcut(pendingShortcut).ok}
        onClick={() => save(pendingShortcut)}>保存</button>
    </div>}
  </div>;
}
