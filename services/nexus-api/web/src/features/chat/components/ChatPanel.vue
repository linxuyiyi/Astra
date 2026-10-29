<template>
  <div
    class="chat-panel"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent="onDragOver"
    @dragleave="onDragLeave"
    @drop.prevent="onDrop"
  >
    <div class="chat-messages-shell">
    <div ref="messageBox" class="chat-messages" data-testid="chat-messages">
      <div v-if="state.status === 'error'" class="fail-bar" role="alert" data-testid="fail-bar">
        <span class="fail-bar__text">{{ state.error }}</span>
        <button type="button" class="fail-bar__retry" data-testid="fail-retry" @click="$emit('retry')">重试</button>
      </div>
      <div
        v-else-if="!state.messages.length && !state.liveLines.length && state.status === 'ready'"
        class="chat-empty"
        data-testid="chat-empty"
      >
        <div class="chat-empty__title">开始对话</div>
        <p class="chat-empty__hint">直接输入消息，或拖入、粘贴图片。Enter 发送 · Shift+Enter 换行 · ⌘K 聚焦输入。</p>
        <ul class="chat-empty__tips">
          <li>可 @ 数字员工（群聊）</li>
          <li>点击麦克风语音转文字</li>
          <li>敏感操作会弹出确认卡</li>
        </ul>
      </div>
      <div
        v-for="message in state.messages"
        :key="message.id"
        class="msg"
        :class="{ user: message.sender === 'user' }"
      >
        <span class="avatar">{{ message.sender === 'user' ? '我' : avatar }}</span>
        <div class="msg-body">
          <div v-if="senderLabel(message)" class="msg-sender-name muted">{{ senderLabel(message) }}</div>
          <div v-if="message.text" class="bubble markdown" v-html="renderMarkdown(message.text)" />
          <template v-for="tool in message.tools || []" :key="tool.id">
            <HitlCard v-if="tool.state === 'asking'" :tool="asLiveTool(tool)" @confirm="(choice) => onHitl(tool.id, choice)" />
            <details v-else class="tool-card" :open="!shouldCollapse(tool.output || tool.input)">
              <summary>{{ tool.name || '工具' }} · {{ tool.state || 'done' }}</summary>
              <pre v-if="tool.input">{{ tool.input }}</pre>
              <pre v-if="tool.output">{{ tool.output }}</pre>
            </details>
          </template>
          <div v-if="message.attachments?.length" class="message-attachments">
            <template v-for="file in message.attachments" :key="file.id">
              <a
                v-if="imageFile(file.type)"
                class="sent-attachment"
                :href="attachmentUrl(file.id)"
                target="_blank"
                rel="noreferrer"
              >
                <img :src="attachmentUrl(file.id)" :alt="file.name" />
              </a>
              <details
                v-else-if="textFile(file.type)"
                class="sent-attachment text-preview"
                :open="previewOpen[file.id] !== false"
              >
                <summary>
                  <a :href="attachmentUrl(file.id)" target="_blank" rel="noreferrer" @click.stop>{{ file.name }}</a>
                  <span class="muted"> · 正文预览</span>
                </summary>
                <pre v-if="previewLoading[file.id]" class="attach-preview muted">加载中…</pre>
                <pre v-else-if="previewError[file.id]" class="attach-preview muted">{{ previewError[file.id] }}</pre>
                <div
                  v-else-if="file.type.includes('markdown') || file.name.endsWith('.md')"
                  class="attach-preview markdown"
                  v-html="renderMarkdown(previewText[file.id] || '')"
                />
                <pre v-else class="attach-preview">{{ previewText[file.id] }}</pre>
              </details>
              <a
                v-else
                class="sent-attachment"
                :href="attachmentUrl(file.id)"
                target="_blank"
                rel="noreferrer"
              >
                <span>{{ file.name }}</span>
              </a>
            </template>
          </div>
        </div>
      </div>
      <div v-for="line in state.liveLines" :key="`live-${line.employeeId}`" class="msg">
        <span class="avatar">{{ avatar }}</span>
        <div class="msg-body">
          <div class="muted">{{ line.name }} · 正在回复</div>
          <div v-if="line.text" class="bubble markdown streaming-bubble">
            <div class="markdown-body" v-html="renderMarkdown(line.text)" />
            <span class="stream-cursor" aria-hidden="true" />
          </div>
          <div v-else class="bubble muted streaming-placeholder">
            <span class="stream-pulse" aria-hidden="true" />
            正在生成…
          </div>
          <template v-for="tool in line.tools" :key="tool.id">
            <HitlCard v-if="tool.state === 'asking'" :tool="tool" @confirm="(choice) => onHitl(tool.id, choice)" />
            <details v-else class="tool-card" open>
              <summary>{{ tool.name || '工具' }} · {{ tool.state || 'running' }}</summary>
              <pre v-if="tool.input">{{ tool.input }}</pre>
              <pre v-if="tool.output">{{ tool.output }}</pre>
            </details>
          </template>
        </div>
      </div>
      <div
        v-if="runtime.streaming && !state.liveLines.length && !state.notice"
        class="msg"
        data-testid="streaming-placeholder"
      >
        <span class="avatar">{{ avatar }}</span>
        <div class="msg-body">
          <div class="muted">{{ target?.name || '数字员工' }} · 正在回复</div>
          <div class="bubble muted streaming-placeholder">
            <span class="stream-pulse" aria-hidden="true" />
            正在生成…
          </div>
        </div>
      </div>
      <div v-if="state.notice" class="fail-bar" role="status" data-testid="fail-bar">
        <span class="fail-bar__text">{{ state.notice }}</span>
        <button
          v-if="state.failedSend || state.noticeSource === 'send'"
          type="button"
          class="fail-bar__retry"
          data-testid="fail-retry"
          :disabled="state.sending"
          @click="retryFailed"
        >重试</button>
        <button
          v-else-if="state.noticeSource === 'stop'"
          type="button"
          class="fail-bar__dismiss"
          data-testid="fail-dismiss"
          @click="runtime.clearNotice()"
        >知道了</button>
        <button
          v-else
          type="button"
          class="fail-bar__retry"
          data-testid="fail-retry"
          @click="$emit('retry')"
        >重试</button>
      </div>
    </div>
    <button
      v-if="showJumpLatest"
      type="button"
      class="jump-latest"
      data-testid="jump-latest"
      @click="jumpToLatest"
    >最新</button>
    </div>

    <div v-if="target" class="chat-compose" data-testid="chat-compose">
      <div class="compose-shell" :class="{ 'is-drop-target': isDropTarget }" data-testid="compose-shell">
        <div class="compose-box" data-testid="compose-box">
          <div
            v-if="(state.draftPending || []).length || state.draftFiles.length"
            class="draft-attachments"
            data-testid="draft-attachments"
          >
            <div
              v-for="file in (state.draftPending || [])"
              :key="file.id"
              class="draft-file draft-file--pending"
              data-testid="draft-chip-pending"
            >
              <span class="draft-file__media" aria-hidden="true">
                <Loader2 class="draft-file__spin" :size="14" />
              </span>
              <span class="draft-file__meta">
                <span class="draft-file__name">{{ file.name }}</span>
                <span class="draft-file__desc">上传中</span>
              </span>
            </div>
            <div
              v-for="file in state.draftFiles"
              :key="file.id"
              class="draft-file"
              data-testid="draft-chip"
            >
              <span class="draft-file__media" aria-hidden="true">
                <img v-if="imageFile(file.type)" :src="attachmentUrl(file.id)" :alt="file.name" />
                <FileText v-else class="draft-file__icon" :size="14" />
              </span>
              <span class="draft-file__meta">
                <span class="draft-file__name">{{ file.name }}</span>
                <span class="draft-file__desc">{{ fileExtLabel(file) }}</span>
              </span>
              <button
                type="button"
                class="draft-file__remove"
                :aria-label="`移除 ${file.name}`"
                @click="runtime.removeDraftFile(file.id)"
              >
                <X :size="14" />
              </button>
            </div>
          </div>

          <div
            v-if="asrState !== 'idle' || asrError || asrRequesting"
            class="asr-status"
            data-testid="asr-status"
            role="status"
          >
            <span v-if="asrRequesting" class="muted">正在请求麦克风…</span>
            <span v-else-if="asrState === 'recording'" class="asr-recording">
              录音中 {{ formatClock(asrSeconds) }}，再次点击结束
            </span>
            <span v-else-if="asrState === 'transcribing'" class="muted">正在识别…</span>
            <span v-else-if="asrState === 'error'" class="asr-error" role="alert">{{ asrError }}</span>
            <button v-if="asrState === 'recording'" type="button" class="asr-text-btn" @click="asrCancel()">取消</button>
            <button v-if="asrState === 'error'" type="button" class="asr-text-btn asr-text-btn--primary" @click="asrRetry()">重试</button>
          </div>

          <div class="compose-row">
            <textarea
              ref="inputEl"
              v-model="state.draft"
              rows="1"
              :placeholder="placeholder"
              :disabled="!canType"
              aria-label="消息输入"
              data-testid="compose-input"
              @keydown="onKeydown"
              @paste="onPaste"
            />
            <div class="compose-actions" data-testid="compose-actions">
              <el-tooltip :content="micTooltip" placement="top">
                <button
                  type="button"
                  class="compose-icon-btn asr-mic"
                  :class="{ recording: asrState === 'recording' }"
                  data-testid="asr-mic"
                  :aria-label="asrState === 'recording' ? '停止录音' : '语音输入（转文字）'"
                  :disabled="!canType || asrState === 'transcribing'"
                  @click="asrToggle()"
                >
                  <Loader2 v-if="asrState === 'transcribing' || asrRequesting" class="spin" :size="16" />
                  <Square v-else-if="asrState === 'recording'" :size="14" />
                  <Mic v-else :size="16" />
                </button>
              </el-tooltip>
              <el-tooltip content="添加附件" placement="top">
                <button
                  type="button"
                  class="compose-icon-btn"
                  data-testid="compose-attach"
                  aria-label="添加附件"
                  :disabled="!canType || state.uploading"
                  @click="pickFiles"
                >
                  <Paperclip :size="16" />
                </button>
              </el-tooltip>
              <el-button
                v-if="runtime.streaming"
                class="compose-send"
                type="danger"
                round
                :loading="state.phase === 'interrupting'"
                data-testid="compose-stop"
                @click="runtime.stop()"
              >停止</el-button>
              <el-button
                v-else
                class="compose-send"
                type="primary"
                round
                data-testid="compose-send"
                :disabled="!canSend"
                :loading="state.sending || state.uploading"
                @click="doSend"
              >发送</el-button>
            </div>
          </div>
        </div>
      </div>
      <div class="compose-hint muted" data-testid="compose-hint">Enter 发送 · Shift+Enter 换行 · ⌘K 聚焦输入</div>
      <input ref="fileInput" type="file" multiple hidden accept="image/*,application/pdf,text/*" @change="onFileChange" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { FileText, Loader2, Mic, Paperclip, Square, X } from "lucide-vue-next";
import { attachmentUrl, currentUid } from "@/api";
import type { Attachment, ChatMessage } from "@/types";
import { ChatRuntime, imageFile } from "../ChatRuntime";
import { renderMarkdown, shouldCollapse } from "../markdown";
import type { ChatSessionState, ChatTarget, LiveTool } from "../types";
import { formatClock, useAsrRecorder } from "../useAsrRecorder";
import HitlCard from "./HitlCard.vue";

const props = defineProps<{
  state: ChatSessionState;
  target: ChatTarget | null;
  runtime: ChatRuntime;
}>();
defineEmits<{ retry: [] }>();

const messageBox = ref<HTMLElement | null>(null);
const inputEl = ref<HTMLTextAreaElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const followLatest = ref(true);
const showJumpLatest = ref(false);
const isDropTarget = ref(false);
let dragDepth = 0;
const SCROLL_BOTTOM_THRESHOLD = 80;
const avatar = computed(() => props.target?.avatar || "员");
const canType = computed(() => Boolean(props.target && props.state.adapter?.canSend && props.state.status === "ready"));
const canSend = computed(
  () =>
    canType.value
    && !props.state.sending
    && !props.state.uploading
    && !(props.state.draftPending || []).length
    && Boolean(props.state.draft.trim() || props.state.draftFiles.length),
);
const placeholder = computed(() =>
  props.target?.kind === "group"
    ? "发送消息，@数字员工，或拖入、粘贴图片…"
    : "发送消息，或拖入、粘贴图片…",
);
const sessionKey = computed(() => props.state.key);
const asrEnabled = computed(() => canType.value);
const {
  state: asrState,
  seconds: asrSeconds,
  error: asrError,
  requesting: asrRequesting,
  toggle: asrToggle,
  cancel: asrCancel,
  retry: asrRetry,
} = useAsrRecorder({
  enabled: asrEnabled,
  sessionKey,
  onText: (text) => {
    const draft = props.state.draft.trim();
    props.state.draft = draft ? `${draft} ${text}` : text;
    void nextTick(() => inputEl.value?.focus());
  },
});
const micTooltip = computed(() => {
  if (asrState.value === "recording") return `录音中 ${formatClock(asrSeconds.value)}`;
  if (asrState.value === "transcribing") return "正在识别…";
  if (asrError.value) return asrError.value;
  return "语音输入（转文字）";
});

const previewText = reactive<Record<string, string>>({});
const previewLoading = reactive<Record<string, boolean>>({});
const previewError = reactive<Record<string, string>>({});
const previewOpen = reactive<Record<string, boolean>>({});
const previewFetched = new Set<string>();

function textFile(type: string) {
  const lower = (type || "").toLowerCase();
  return lower.startsWith("text/") || lower === "application/markdown" || lower.includes("markdown");
}

function fileExtLabel(file: Attachment) {
  const fromName = file.name.includes(".") ? file.name.split(".").pop() : "";
  if (fromName && fromName.length <= 8) return fromName.toUpperCase();
  const type = (file.type || "").split("/").pop() || "FILE";
  return type.toUpperCase();
}

async function ensurePreview(file: Attachment) {
  if (!textFile(file.type) || previewFetched.has(file.id)) return;
  previewFetched.add(file.id);
  previewLoading[file.id] = true;
  previewError[file.id] = "";
  try {
    const response = await fetch(attachmentUrl(file.id), { credentials: "same-origin" });
    if (!response.ok) throw new Error(`无法加载附件（HTTP ${response.status}）`);
    const text = await response.text();
    previewText[file.id] = text.length > 20_000 ? `${text.slice(0, 20_000)}\n…（已截断）` : text;
    if (previewOpen[file.id] === undefined) previewOpen[file.id] = text.length < 4000;
  } catch (error) {
    previewError[file.id] = error instanceof Error ? error.message : "附件预览失败";
  } finally {
    previewLoading[file.id] = false;
  }
}

function senderLabel(message: ChatMessage) {
  const speaker = message.speaker || "";
  const me = currentUid();
  const isSelf =
    message.sender === "user" && (!speaker || speaker === "user" || (me && speaker === me));
  // 一对一：自己气泡不显示「你」名行；群聊仍显示说话人名（含自己）。
  if (isSelf) return props.target?.kind === "group" ? "你" : "";
  const identity = speaker && speaker !== "user" ? speaker : message.sender;
  if (identity === props.target?.id) return props.target?.name || "数字员工";
  return identity || "数字员工";
}

function asLiveTool(tool: { id: string; name: string; input: string; output: string; state: string }): LiveTool {
  return { id: tool.id, name: tool.name, input: tool.input, output: tool.output, state: tool.state };
}

function onHitl(toolId: string, choice: "yes" | "yes_with_rule" | "no") {
  // 壳内接线：更新 asking→allowed/finished。真正放行仍走既有 HITL/PR#12，不另开通道。
  props.runtime.resolveHitl(toolId, choice);
}

async function doSend() {
  followLatest.value = true;
  await props.runtime.send(props.state.draft, props.state.draftFiles);
  await nextTick();
  scrollBottom();
  inputEl.value?.focus();
}

function pickFiles() {
  fileInput.value?.click();
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  if (input.files?.length) void props.runtime.attach(input.files);
  input.value = "";
}

function onDragEnter(event: DragEvent) {
  if (!canType.value) return;
  if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
  dragDepth += 1;
  isDropTarget.value = true;
}

function onDragOver(event: DragEvent) {
  if (!canType.value) return;
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  isDropTarget.value = true;
}

function onDragLeave() {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) isDropTarget.value = false;
}

function onDrop(event: DragEvent) {
  dragDepth = 0;
  isDropTarget.value = false;
  if (!canType.value) return;
  const files = event.dataTransfer?.files;
  if (files?.length) void props.runtime.attach(files);
}

function onPaste(event: ClipboardEvent) {
  const files = event.clipboardData?.files;
  if (files?.length) {
    event.preventDefault();
    void props.runtime.attach(files);
  }
}

function onKeydown(event: KeyboardEvent) {
  // IME 组字中（isComposing / keyCode 229）不发送，Shift+Enter 换行。
  if (event.key !== "Enter" || event.shiftKey) return;
  if (event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (canSend.value) void doSend();
}

function focusInput(event: KeyboardEvent) {
  const meta = event.metaKey || event.ctrlKey;
  if (!meta || event.key.toLowerCase() !== "k") return;
  if ((event.target as HTMLElement | null)?.closest?.("input, textarea, [contenteditable]")) {
    // 已在可编辑区时仍抢焦点到本会话输入，符合 ⌘K 直达。
  }
  event.preventDefault();
  inputEl.value?.focus();
}

function distanceFromBottom(box: HTMLElement) {
  return box.scrollHeight - box.scrollTop - box.clientHeight;
}

function onMessagesScroll() {
  const box = messageBox.value;
  if (!box) return;
  const dist = distanceFromBottom(box);
  const atBottom = dist <= SCROLL_BOTTOM_THRESHOLD;
  followLatest.value = atBottom;
  showJumpLatest.value = !atBottom;
}

function scrollBottom() {
  const box = messageBox.value;
  if (!box) return;
  box.scrollTop = box.scrollHeight;
  followLatest.value = true;
  showJumpLatest.value = false;
}

function jumpToLatest() {
  followLatest.value = true;
  showJumpLatest.value = false;
  scrollBottom();
}

async function retryFailed() {
  const ok = await props.runtime.retryFailedSend();
  if (ok) {
    await nextTick();
    jumpToLatest();
    inputEl.value?.focus();
  }
}

function onCodeCopyClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null;
  const btn = target?.closest?.("[data-md-copy]") as HTMLElement | null;
  if (!btn) return;
  event.preventDefault();
  const block = btn.closest(".md-codeblock");
  const pre = block?.querySelector("pre");
  const text = pre?.textContent ?? "";
  if (!text) return;
  void navigator.clipboard.writeText(text).then(() => {
    const prev = btn.textContent;
    btn.textContent = "Copied";
    btn.classList.add("is-copied");
    window.setTimeout(() => {
      btn.textContent = prev || "Copy";
      btn.classList.remove("is-copied");
    }, 1600);
  });
}

watch(
  () => [props.state.messages.length, props.state.liveLines.map((l) => l.text).join(""), props.state.notice, props.state.phase],
  async () => {
    if (!followLatest.value) {
      showJumpLatest.value = true;
      return;
    }
    await nextTick();
    scrollBottom();
  },
);

watch(
  () => props.state.messages.map((message) => (message.attachments || []).map((file) => `${file.id}:${file.type}`).join(",")).join("|"),
  () => {
    for (const message of props.state.messages) {
      for (const file of message.attachments || []) {
        if (textFile(file.type)) void ensurePreview(file);
      }
    }
  },
  { immediate: true },
);

onMounted(() => {
  window.addEventListener("keydown", focusInput);
  messageBox.value?.addEventListener("scroll", onMessagesScroll, { passive: true });
  messageBox.value?.addEventListener("click", onCodeCopyClick);
  scrollBottom();
  inputEl.value?.focus();
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", focusInput);
  messageBox.value?.removeEventListener("scroll", onMessagesScroll);
  messageBox.value?.removeEventListener("click", onCodeCopyClick);
});
</script>

<style scoped>
.chat-panel { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.chat-messages-shell {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.chat-messages {
  flex: 1;
  padding: 24px 20px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 17px;
}
.jump-latest {
  position: absolute;
  right: 20px;
  bottom: 16px;
  z-index: 5;
  border: 1px solid #a8cbb8;
  background: #fff;
  color: #188b69;
  font-size: 12px;
  font-weight: 650;
  border-radius: 999px;
  padding: 7px 14px;
  box-shadow: 0 8px 22px rgba(24, 139, 105, 0.16);
  cursor: pointer;
}
.jump-latest:hover { background: #eef8f2; border-color: #188b69; }
.fail-bar {
  width: 100%;
  max-width: 760px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 12px;
  border: 1px solid #f0c7b8;
  background: #fff6f2;
  color: #8a3b22;
  box-shadow: 0 6px 18px rgba(176, 74, 42, 0.08);
}
.fail-bar__text {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
}
.fail-bar__retry,
.fail-bar__dismiss {
  flex: none;
  border: 0;
  border-radius: 999px;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.fail-bar__retry {
  background: #188b69;
  color: #fff;
}
.fail-bar__retry:hover:not(:disabled) { background: #147556; }
.fail-bar__retry:disabled { opacity: 0.55; cursor: not-allowed; }
.fail-bar__dismiss {
  background: transparent;
  color: #8a3b22;
  text-decoration: underline;
  text-underline-offset: 2px;
}
.chat-empty {
  margin-top: auto;
  margin-bottom: auto;
  padding: 36px 24px 28px;
  text-align: center;
  border: 1px dashed #cfe3d8;
  border-radius: 16px;
  background: #fbfefc;
  box-shadow: 0 8px 24px rgba(24, 139, 105, 0.04);
}
.chat-empty__title {
  font-size: 16px;
  font-weight: 750;
  color: var(--ink, #203830);
  margin-bottom: 8px;
}
.chat-empty__hint {
  margin: 0 auto 14px;
  max-width: 420px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--muted, #7b9386);
}
.chat-empty__tips {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}
.chat-empty__tips li {
  font-size: 12px;
  color: #3f6b56;
  background: #eef8f2;
  border: 1px solid #d5eade;
  border-radius: 999px;
  padding: 5px 11px;
}
.msg-body { min-width: 0; display: grid; gap: 6px; flex: 1; }
.msg-sender-name {
  font-size: 11px;
  font-weight: 600;
  line-height: 1.3;
  margin-bottom: -2px; /* with msg-body gap 6 → ~4px to bubble */
}
.chat-compose {
  padding: 14px 20px 18px;
  border-top: 1px solid #edf2ef;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.compose-shell { width: 100%; max-width: 760px; border-radius: 30px; transition: box-shadow 0.15s ease; }
.compose-shell.is-drop-target {
  outline: 2px dashed var(--green, #188b69);
  outline-offset: 3px;
  box-shadow: 0 0 0 4px rgba(24, 139, 105, 0.12);
}
.compose-shell.is-drop-target :deep(.compose-box),
.compose-shell.is-drop-target .compose-box {
  border-color: var(--green, #188b69);
  border-style: dashed;
}
.compose-hint { margin-top: 8px; font-size: 11px; width: 100%; max-width: 760px; }
.compose-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  min-height: 52px;
}
.compose-row textarea {
  flex: 1;
  min-width: 0;
  border: 0;
  resize: none;
  outline: none;
  background: transparent;
  min-height: 52px;
  max-height: 168px;
  padding: 15.5px 4px;
  line-height: 21px;
  font-size: 14px;
  color: var(--ink, #203830);
}
.compose-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 52px;
  flex-shrink: 0;
}
.compose-icon-btn {
  width: 36px;
  height: 36px;
  border-radius: 999px;
  border: 0;
  background: transparent;
  color: #4d6b5c;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.compose-icon-btn:hover:not(:disabled) {
  background: #eef6f1;
  color: #188b69;
}
.compose-icon-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.compose-icon-btn .spin { animation: compose-spin 0.9s linear infinite; }
.compose-send {
  min-width: 64px;
  height: 36px;
  padding: 0 14px;
  font-weight: 650;
}
.draft-attachments {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  overscroll-behavior-x: contain;
  scrollbar-width: none;
  padding: 4px 2px 2px;
  margin: 0 0 2px;
  max-width: 100%;
}
.draft-attachments::-webkit-scrollbar { display: none; }
.draft-file {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
  min-width: 148px;
  max-width: 220px;
  border: 1px solid #dcebe1;
  border-radius: 12px;
  padding: 6px 8px;
  background: #f7fbf8;
}
.draft-file--pending {
  border-style: dashed;
  opacity: 0.92;
}
.draft-file__media {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #e8f4ee;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex: none;
  color: #3f6b56;
}
.draft-file__media img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.draft-file__spin { animation: compose-spin 0.9s linear infinite; }
.draft-file__meta {
  min-width: 0;
  flex: 1;
  display: grid;
  gap: 1px;
}
.draft-file__name {
  font-size: 12px;
  font-weight: 600;
  color: var(--ink, #203830);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.draft-file__desc {
  font-size: 11px;
  color: var(--muted, #7b9386);
}
.draft-file__remove {
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: #6b8979;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex: none;
}
.draft-file__remove:hover { background: #e8f0eb; color: #203830; }
.asr-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 2px 4px 0;
  font-size: 12px;
}
.asr-recording { color: #c0392b; font-weight: 600; }
.asr-error { color: #b42318; }
.asr-text-btn {
  border: 0;
  background: transparent;
  color: #3f6b56;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
  font-size: 12px;
  padding: 0;
}
.asr-text-btn--primary { color: #188b69; font-weight: 600; }
.asr-mic.recording {
  background: #e74c3c;
  color: #fff;
}
.asr-mic.recording:hover:not(:disabled) {
  background: #d64537;
  color: #fff;
}
@keyframes compose-spin {
  to { transform: rotate(360deg); }
}
@media (prefers-reduced-motion: reduce) {
  .compose-icon-btn .spin, .draft-file__spin { animation: none; }
}
.tool-card {
  border: 1px solid #e1ece5;
  border-radius: 10px;
  padding: 6px 10px;
  background: #f7fbf8;
  font-size: 12px;
}
.tool-card summary { cursor: pointer; color: #3f6b56; }
.tool-card pre {
  margin: 6px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  overflow: auto;
}
/* GFM inside bubbles: override .bubble pre-wrap so MD HTML does not keep source newlines as gaps. */
.markdown {
  white-space: normal;
  overflow-x: auto;
}
.attach-preview.markdown {
  white-space: normal;
}
.markdown :deep(p) { margin: 0 0 0.55em; }
.markdown :deep(p:last-child) { margin-bottom: 0; }
.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4),
.markdown :deep(h5),
.markdown :deep(h6) {
  margin: 0.7em 0 0.35em;
  line-height: 1.35;
  font-weight: 700;
  color: var(--ink, #203830);
}
.markdown :deep(h1:first-child),
.markdown :deep(h2:first-child),
.markdown :deep(h3:first-child),
.markdown :deep(h4:first-child),
.markdown :deep(h5:first-child),
.markdown :deep(h6:first-child) { margin-top: 0; }
.markdown :deep(h1) { font-size: 1.28em; } /* ~16–18px in bubble */
.markdown :deep(h2) { font-size: 1.16em; font-weight: 650; }
.markdown :deep(h3) { font-size: 1.08em; font-weight: 650; }
.markdown :deep(h4),
.markdown :deep(h5),
.markdown :deep(h6) { font-size: 1em; font-weight: 650; }
.markdown :deep(ul),
.markdown :deep(ol) {
  margin: 0.35em 0 0.55em;
  padding-left: 1.45em;
}
.markdown :deep(ul) { list-style: disc outside; }
.markdown :deep(ol) { list-style: decimal outside; }
.markdown :deep(li) { margin: 0.15em 0; }
.markdown :deep(li > ul),
.markdown :deep(li > ol) { margin: 0.15em 0; }
.markdown :deep(a) {
  color: #188b69;
  text-decoration: underline;
  text-underline-offset: 2px;
}
.markdown :deep(a:hover) { color: #0f6e52; }
.markdown :deep(blockquote) {
  margin: 0.5em 0;
  padding: 0.25em 0 0.25em 0.85em;
  border-left: 3px solid #a6d4bb;
  color: #5a7466;
  background: transparent;
}
.markdown :deep(blockquote p) { margin: 0.25em 0; }
.markdown :deep(table) {
  border-collapse: collapse;
  width: max-content;
  max-width: 100%;
  margin: 0.5em 0;
  font-size: 0.92em;
}
.markdown :deep(th),
.markdown :deep(td) {
  border: 1px solid #c5d9cd;
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
}
.markdown :deep(th) {
  background: #eef6f1;
  font-weight: 650;
  color: var(--ink, #203830);
}
.markdown :deep(tr:nth-child(even) td) { background: #f7fbf8; }
.markdown :deep(hr) {
  border: 0;
  border-top: 1px solid #d5eade;
  margin: 0.75em 0;
}
.markdown :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.92em;
}
/* 刀 B：代码块横滑 + 右上 Copy（不改 h1–h6/ul/blockquote/table） */
.markdown :deep(.md-codeblock) {
  position: relative;
  margin: 8px 0;
  border-radius: 10px;
  background: #eef6f1;
  border: 1px solid #d5eade;
  overflow: hidden;
  max-width: 100%;
}
.markdown :deep(.md-codeblock__pre),
.markdown :deep(pre.md-codeblock__pre) {
  margin: 0;
  padding: 12px 14px;
  padding-top: 36px;
  overflow-x: auto;
  overflow-y: auto;
  max-height: 280px;
  white-space: pre;
  background: transparent;
  border-radius: 0;
}
.markdown :deep(.md-codeblock__copy) {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 1;
  border: 1px solid #c5d9cd;
  background: #fff;
  color: #3f6b56;
  border-radius: 8px;
  font-size: 11px;
  font-weight: 650;
  padding: 3px 8px;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(32, 56, 48, 0.06);
}
.markdown :deep(.md-codeblock__copy:hover) { border-color: #188b69; color: #188b69; }
.markdown :deep(.md-codeblock__copy.is-copied) {
  background: #188b69;
  border-color: #188b69;
  color: #fff;
}
.markdown :deep(pre:not(.md-codeblock__pre)) {
  margin: 8px 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: #eef6f1;
  overflow-x: auto;
  max-height: 280px;
  white-space: pre;
}
.markdown :deep(pre code) {
  font-size: 0.9em;
  background: transparent;
}
.sent-attachment.image { display: flex; gap: 8px; align-items: center; }
.sent-attachment img { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; }
.text-preview {
  display: block;
  max-width: min(520px, 100%);
  padding: 8px 10px;
}
.text-preview summary { cursor: pointer; }
.attach-preview {
  margin: 8px 0 0;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  background: #f7fbf8;
  border-radius: 8px;
  padding: 8px 10px;
}
.streaming-placeholder {
  font-style: italic;
  opacity: 0.95;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 1.6em;
}
.streaming-bubble { position: relative; }
.streaming-bubble .markdown-body { display: inline; }
.stream-cursor {
  display: inline-block;
  width: 2px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: -0.12em;
  background: var(--green, #188b69);
  border-radius: 1px;
  animation: stream-blink 1s step-end infinite;
  box-shadow: 0 0 8px rgba(24, 139, 105, 0.45);
}
.stream-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--green, #188b69);
  box-shadow: 0 0 0 0 rgba(24, 139, 105, 0.45);
  animation: stream-pulse 1.2s ease-out infinite;
  flex: none;
}
@keyframes stream-blink {
  50% { opacity: 0; }
}
@keyframes stream-pulse {
  0% { transform: scale(0.85); box-shadow: 0 0 0 0 rgba(24, 139, 105, 0.45); }
  70% { transform: scale(1); box-shadow: 0 0 0 10px rgba(24, 139, 105, 0); }
  100% { transform: scale(0.85); box-shadow: 0 0 0 0 rgba(24, 139, 105, 0); }
}
@media (prefers-reduced-motion: reduce) {
  .stream-cursor, .stream-pulse { animation: none; }
  .stream-pulse { box-shadow: none; }
}
</style>
