<template>
  <div
    class="msg-footer"
    :class="{ 'is-user': variant === 'user', 'is-assistant': variant === 'assistant' }"
    data-testid="msg-footer"
  >
    <div
      v-if="variant === 'assistant'"
      class="msg-footer__badge"
      :aria-label="running ? '正在生成' : undefined"
      data-testid="msg-footer-status"
    >
      <Loader2 v-if="running" class="msg-footer__icon spin" :size="12" aria-hidden="true" />
      <CheckCircle2 v-else class="msg-footer__icon" :size="12" aria-hidden="true" />
      <span v-if="elapsedText" class="msg-footer__elapsed tabular" data-testid="msg-footer-elapsed">{{ elapsedText }}</span>
      <template v-if="hasUsage">
        <ArrowUp class="msg-footer__icon msg-footer__gap" :size="11" aria-hidden="true" />
        <span class="tabular" data-testid="msg-footer-in">{{ formatNumber(inputTokens) }}</span>
        <ArrowDown class="msg-footer__icon msg-footer__gap" :size="11" aria-hidden="true" />
        <span class="tabular" data-testid="msg-footer-out">{{ formatNumber(outputTokens) }}</span>
      </template>
    </div>
    <button
      v-if="plainText"
      type="button"
      class="msg-footer__copy"
      :class="{ 'is-copied': copied }"
      data-testid="msg-footer-copy"
      :aria-label="copied ? '已复制' : '复制'"
      :title="copied ? '已复制' : '复制'"
      @click="onCopy"
    >
      <Check v-if="copied" :size="13" aria-hidden="true" />
      <Copy v-else :size="13" aria-hidden="true" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ArrowDown, ArrowUp, Check, CheckCircle2, Copy, Loader2 } from "lucide-vue-next";
import {
  formatNumber,
  formatTime,
  isMessageRunning,
  messageElapsedSeconds,
  messagePlainText,
} from "../messageFormat";

const props = withDefaults(
  defineProps<{
    variant: "user" | "assistant";
    text?: string;
    createdAt?: string | null;
    finishedAt?: string | null;
    usage?: { input_tokens?: number; output_tokens?: number } | null;
    /** Force running (live line); otherwise derive from finishedAt === null. */
    forceRunning?: boolean;
  }>(),
  { text: "", forceRunning: false },
);

const plainText = computed(() => messagePlainText(props.text));
const running = computed(() => props.forceRunning || isMessageRunning(props.finishedAt));
const inputTokens = computed(() => props.usage?.input_tokens ?? 0);
const outputTokens = computed(() => props.usage?.output_tokens ?? 0);
const hasUsage = computed(
  () => !!props.usage && (inputTokens.value > 0 || outputTokens.value > 0),
);

const nowMs = ref(Date.now());
let tickTimer: number | null = null;

function clearTick() {
  if (tickTimer != null) {
    window.clearInterval(tickTimer);
    tickTimer = null;
  }
}

function syncTick() {
  clearTick();
  if (!running.value) return;
  nowMs.value = Date.now();
  tickTimer = window.setInterval(() => {
    nowMs.value = Date.now();
  }, 1000);
}

onMounted(syncTick);
onBeforeUnmount(clearTick);
watch(running, syncTick);

const elapsedText = computed(() => {
  const seconds = messageElapsedSeconds({
    createdAt: props.createdAt,
    finishedAt: props.finishedAt,
    nowMs: nowMs.value,
    running: running.value,
  });
  if (seconds == null) return "";
  return formatTime(seconds);
});

const copied = ref(false);
let copiedTimer: number | null = null;

async function onCopy() {
  const text = plainText.value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    return;
  }
  copied.value = true;
  if (copiedTimer != null) window.clearTimeout(copiedTimer);
  copiedTimer = window.setTimeout(() => {
    copied.value = false;
    copiedTimer = null;
  }, 2000);
}

onBeforeUnmount(() => {
  if (copiedTimer != null) window.clearTimeout(copiedTimer);
});
</script>

<style scoped>
.msg-footer {
  display: flex;
  align-items: center;
  gap: 4px;
  min-height: 22px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 11px;
  color: var(--muted, #7b9386);
}
.msg-footer.is-user {
  justify-content: flex-end;
}
.msg-footer__badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef4f0;
  color: #5a7366;
  border: 1px solid #d7e4dc;
  letter-spacing: -0.02em;
  line-height: 1.4;
}
.msg-footer__icon {
  flex: none;
  opacity: 0.85;
}
.msg-footer__gap {
  margin-left: 2px;
}
.msg-footer__elapsed {
  margin-left: 1px;
}
.tabular {
  font-variant-numeric: tabular-nums;
}
.spin {
  animation: msg-footer-spin 0.9s linear infinite;
}
@keyframes msg-footer-spin {
  to { transform: rotate(360deg); }
}
.msg-footer__copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #6a8576;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease, background 0.12s ease, color 0.12s ease;
}
.msg-footer__copy:hover {
  background: #eef6f1;
  color: #188b69;
}
.msg-footer__copy:focus-visible,
.msg-footer__copy.is-copied {
  opacity: 1;
}
.msg-footer__copy.is-copied {
  color: #188b69;
}
@media (hover: none) {
  .msg-footer__copy { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
}
</style>
