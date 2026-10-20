<template>
  <div class="nebula-summary" aria-label="运行概览">
    <div class="summary-metrics">
      <div v-for="item in tiles" :key="item.label" class="summary-tile" :class="item.tone">
        <small>{{ item.label }}</small>
        <strong>{{ item.value }}</strong>
        <em v-if="item.note">{{ item.note }}</em>
      </div>
    </div>
    <div class="summary-connection">
      <span class="connection-dot" :data-state="connection"></span>
      <span>{{ connectionText }}</span>
      <span v-if="lastConfirmedAt" class="connection-time">已确认 {{ formatTime(lastConfirmedAt) }}</span>
      <button type="button" class="connection-refresh" :disabled="refreshing" @click="emit('refresh')">
        {{ refreshing ? "刷新中" : "刷新" }}
      </button>
    </div>
    <div v-if="degraded.length" class="summary-degraded">
      部分数据暂不可用（{{ degraded.join("、") }}），保留最近一次已确认状态。
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { formatTime } from "../layout";
import type { NebulaSnapshot } from "../types";

const props = defineProps<{
  snapshot: NebulaSnapshot | null;
  connection: "connecting" | "live" | "stale" | "offline";
  lastConfirmedAt: string;
  refreshing: boolean;
  error: string;
}>();

const emit = defineEmits<{ refresh: [] }>();

const tiles = computed(() => {
  const totals = props.snapshot?.totals;
  return [
    { label: "数字员工星系", value: totals?.employees ?? 0, note: `${totals?.workers ?? 0} Worker`, tone: "" },
    {
      label: "在线 Worker",
      value: totals?.workersOnline ?? 0,
      note: `共 ${totals?.workers ?? 0}`,
      tone: totals && totals.workersOnline === 0 && totals.workers > 0 ? "muted" : "",
    },
    { label: "执行中任务", value: totals?.tasksRunning ?? 0, note: "RUNNING", tone: "" },
    { label: "排队中任务", value: totals?.tasksQueued ?? 0, note: "PENDING", tone: "" },
    {
      label: "待人工",
      value: totals?.tasksWaiting ?? 0,
      note: "仍需占用资源",
      tone: totals?.tasksWaiting ? "warn" : "",
    },
    {
      label: "执行异常",
      value: totals?.tasksFailed ?? 0,
      note: "FAILED",
      tone: totals?.tasksFailed ? "danger" : "",
    },
    { label: "今日完成", value: totals?.completedToday ?? 0, note: "按完成时间", tone: "ok" },
  ];
});

const connectionText = computed(() => {
  if (props.connection === "live") return "实时事件已连接";
  if (props.connection === "stale") return "事件连接中断，显示最近确认状态";
  if (props.connection === "offline") return props.error || "服务不可达";
  return "正在连接实时事件";
});

const degraded = computed(() => props.snapshot?.degradedSources ?? []);
</script>

<style scoped>
.nebula-summary {
  position: absolute;
  left: 26px;
  top: 112px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
  z-index: 3;
  max-width: calc(100% - 40px);
}
.summary-metrics { display: flex; gap: 7px; flex-wrap: wrap; }
.summary-tile {
  min-width: 86px;
  padding: 8px 11px;
  background: #ffffff0b;
  border: 1px solid #ffffff22;
  border-radius: 10px;
  backdrop-filter: blur(9px);
}
.summary-tile small { font-size: 9px; color: #83b8a6; display: block; white-space: nowrap; }
.summary-tile strong { font-size: 20px; color: #f0fff8; font-weight: 700; line-height: 1.42; display: block; }
.summary-tile em { color: #78e8bd; font-size: 9px; font-style: normal; }
.summary-tile.warn strong { color: #ffd0a1; }
.summary-tile.danger strong { color: #ff9fb2; }
.summary-tile.ok strong { color: #a9f4d1; }
.summary-tile.muted strong { color: #c3d6cf; }
.summary-connection {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  align-self: flex-start;
  color: #8fc4b1;
  font-size: 10px;
  background: #052920c9;
  border: 1px solid #ffffff1c;
  border-radius: 9px;
  padding: 6px 9px;
  pointer-events: auto;
  backdrop-filter: blur(10px);
}
.connection-dot { width: 6px; height: 6px; border-radius: 50%; background: #62d9ac; box-shadow: 0 0 9px #62d9ac; }
.connection-dot[data-state="stale"] { background: #f0b26b; box-shadow: 0 0 9px #f0b26b; }
.connection-dot[data-state="offline"] { background: #ff8a9e; box-shadow: 0 0 9px #ff8a9e; }
.connection-dot[data-state="connecting"] { background: #8aa9a1; box-shadow: none; }
.connection-time { color: #6f9d8b; }
.connection-refresh { border: 0; background: #0d483c; color: #d5f4e9; border-radius: 6px; font-size: 9px; padding: 3px 7px; cursor: pointer; }
.connection-refresh:disabled { opacity: .55; cursor: progress; }
.summary-degraded {
  pointer-events: auto;
  align-self: flex-start;
  max-width: 520px;
  border: 1px solid #eccb8c66;
  background: #3a2a0fbb;
  color: #ffe0a8;
  border-radius: 8px;
  padding: 6px 9px;
  font-size: 10px;
  line-height: 1.5;
}
@media (max-width: 1400px) {
  .summary-tile { min-width: 74px; padding: 7px 9px; }
  .summary-tile strong { font-size: 17px; }
}
@media (max-width: 1120px) {
  .nebula-summary { left: 14px; top: 74px; }
  .summary-metrics { max-width: calc(100vw - 440px); }
}
@media (max-width: 900px) {
  .nebula-summary { top: 76px; max-width: calc(100% - 20px); }
  .summary-metrics { display: none; }
  .summary-connection { font-size: 9px; }
}
@media (max-width: 760px) {
  .nebula-summary { top: 72px; }
}
</style>
