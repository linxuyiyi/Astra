<template>
  <div class="chat-panel" @dragover.prevent @drop.prevent="onDrop">
    <div ref="messageBox" class="chat-messages" data-testid="chat-messages">
      <div v-if="state.status === 'error'" class="msg system" role="alert">
        <div class="bubble">{{ state.error }}</div>
        <el-button size="small" @click="$emit('retry')">重试</el-button>
      </div>
      <div
        v-else-if="!state.messages.length && !state.liveLines.length && state.status === 'ready'"
        class="chat-empty muted"
      >
        直接输入消息开始对话
      </div>
      <div
        v-for="message in state.messages"
        :key="message.id"
        class="msg"
        :class="{ user: message.sender === 'user' }"
      >
        <span class="avatar">{{ message.sender === 'user' ? '我' : avatar }}</span>
        <div class="msg-body">
          <div class="muted">{{ senderLabel(message) }}</div>
          <div v-if="message.text" class="bubble markdown" v-html="renderMarkdown(message.text)" />
          <template v-for="tool in message.tools || []" :key="tool.id">
            <HitlCard v-if="tool.state === 'asking'" :tool="asLiveTool(tool)" @confirm="onHitl" />
            <details v-else class="tool-card" :open="!shouldCollapse(tool.output || tool.input)">
              <summary>{{ tool.name || '工具' }} · {{ tool.state || 'done' }}</summary>
              <pre v-if="tool.input">{{ tool.input }}</pre>
              <pre v-if="tool.output">{{ tool.output }}</pre>
            </details>
          </template>
          <div v-if="message.attachments?.length" class="message-attachments">
            <a
              v-for="file in message.attachments"
              :key="file.id"
              class="sent-attachment"
              :href="attachmentUrl(file.id)"
              target="_blank"
              rel="noreferrer"
            >
              <img v-if="imageFile(file.type)" :src="attachmentUrl(file.id)" :alt="file.name" />
              <span v-else>{{ file.name }}</span>
            </a>
          </div>
        </div>
      </div>
      <div v-for="line in state.liveLines" :key="`live-${line.employeeId}`" class="msg">
        <span class="avatar">{{ avatar }}</span>
        <div class="msg-body">
          <div class="muted">{{ line.name }} · 正在回复</div>
          <div v-if="line.text" class="bubble markdown" v-html="renderMarkdown(line.text)" />
          <template v-for="tool in line.tools" :key="tool.id">
            <HitlCard v-if="tool.state === 'asking'" :tool="tool" @confirm="onHitl" />
            <details v-else class="tool-card" open>
              <summary>{{ tool.name || '工具' }} · {{ tool.state || 'running' }}</summary>
              <pre v-if="tool.input">{{ tool.input }}</pre>
              <pre v-if="tool.output">{{ tool.output }}</pre>
            </details>
          </template>
        </div>
      </div>
      <div v-if="state.notice" class="msg system" role="status">
        <div class="bubble">{{ state.notice }}</div>
        <el-button size="small" @click="$emit('retry')">重试</el-button>
      </div>
    </div>

    <div v-if="target" class="chat-compose" data-testid="chat-compose">
      <div v-if="state.draftFiles.length" class="draft-attachments">
        <div v-for="file in state.draftFiles" :key="file.id" class="draft-file">
          <img v-if="imageFile(file.type)" :src="attachmentUrl(file.id)" :alt="file.name" />
          <span>{{ file.name }}</span>
          <el-button text size="small" @click="runtime.removeDraftFile(file.id)">移除</el-button>
        </div>
      </div>
      <div class="compose-box">
        <el-tooltip content="添加附件" placement="top">
          <el-button circle aria-label="添加附件" :disabled="!canType" @click="pickFiles">＋</el-button>
        </el-tooltip>
        <!-- 藏麦：不渲染麦克风入口；⌘K / Ctrl+K 聚焦输入 -->
        <textarea
          ref="inputEl"
          v-model="state.draft"
          rows="1"
          :placeholder="placeholder"
          :disabled="!canType"
          aria-label="消息输入"
          @keydown="onKeydown"
          @paste="onPaste"
        />
        <el-button
          v-if="runtime.streaming"
          type="danger"
          plain
          :loading="state.phase === 'interrupting'"
          @click="runtime.stop()"
        >停止</el-button>
        <el-button
          v-else
          type="primary"
          :disabled="!canSend"
          :loading="state.sending || state.uploading"
          @click="doSend"
        >发送</el-button>
      </div>
      <div class="compose-hint muted">Enter 发送 · Shift+Enter 换行 · ⌘K 聚焦 · 麦克风已隐藏</div>
      <input ref="fileInput" type="file" multiple hidden accept="image/*,application/pdf,text/*" @change="onFileChange" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { attachmentUrl, currentUid } from "@/api";
import type { ChatMessage } from "@/types";
import { ChatRuntime, imageFile } from "../ChatRuntime";
import { renderMarkdown, shouldCollapse } from "../markdown";
import type { ChatSessionState, ChatTarget, LiveTool } from "../types";
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
const avatar = computed(() => props.target?.avatar || "员");
const canType = computed(() => Boolean(props.target && props.state.adapter?.canSend && props.state.status === "ready"));
const canSend = computed(
  () => canType.value && !props.state.sending && Boolean(props.state.draft.trim() || props.state.draftFiles.length),
);
const placeholder = computed(() =>
  props.target?.kind === "group"
    ? "发送消息，@数字员工，或拖入、粘贴图片…"
    : "发送消息，或拖入、粘贴图片…",
);

function senderLabel(message: ChatMessage) {
  const speaker = message.speaker || "";
  const me = currentUid();
  if (message.sender === "user" && (!speaker || speaker === "user" || (me && speaker === me))) return "你";
  const identity = speaker && speaker !== "user" ? speaker : message.sender;
  if (identity === props.target?.id) return props.target?.name || "数字员工";
  return identity || "数字员工";
}

function asLiveTool(tool: { id: string; name: string; input: string; output: string; state: string }): LiveTool {
  return { id: tool.id, name: tool.name, input: tool.input, output: tool.output, state: tool.state };
}

function onHitl(_choice: "yes" | "yes_with_rule" | "no") {
  // 展示接线完成；真正放行仍走既有 HITL/PR#12 机制，此处不另开通道。
}

async function doSend() {
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

function onDrop(event: DragEvent) {
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
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (canSend.value) void doSend();
  }
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

function scrollBottom() {
  const box = messageBox.value;
  if (box) box.scrollTop = box.scrollHeight;
}

watch(
  () => [props.state.messages.length, props.state.liveLines.map((l) => l.text).join(""), props.state.notice],
  async () => {
    await nextTick();
    scrollBottom();
  },
);

onMounted(() => {
  window.addEventListener("keydown", focusInput);
  scrollBottom();
  inputEl.value?.focus();
});
onBeforeUnmount(() => window.removeEventListener("keydown", focusInput));
</script>

<style scoped>
.chat-panel { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.chat-messages { flex: 1; padding: 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 17px; }
.chat-empty { padding: 12px 4px; font-size: 13px; }
.msg-body { min-width: 0; display: grid; gap: 6px; }
.chat-compose { padding: 12px 17px 16px; border-top: 1px solid #edf2ef; }
.compose-hint { margin-top: 6px; font-size: 11px; }
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
.markdown :deep(p) { margin: 0 0 0.6em; }
.markdown :deep(p:last-child) { margin-bottom: 0; }
.markdown :deep(code) { font-family: ui-monospace, monospace; }
.markdown :deep(pre) {
  margin: 8px 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: #eef6f1;
  overflow: auto;
  max-height: 280px;
}
.sent-attachment.image, .draft-file { display: flex; gap: 8px; align-items: center; }
.sent-attachment img, .draft-file img { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; }
</style>
