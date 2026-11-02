<template>
  <div class="tool-activity">
    <HitlCard v-for="tool in confirmations" :key="tool.id" :tool="tool" />
    <details v-if="records.length" class="tool-records" data-testid="tool-activity">
      <summary>
        工具调用（{{ records.length }}）
        <span v-if="running" class="tool-running"> · 进行中</span>
        <span v-if="failures" class="tool-failures"> · {{ failures }} 项失败</span>
      </summary>
      <div class="tool-records__body">
        <details v-for="tool in records" :key="tool.id" class="tool-entry">
          <summary :class="{ 'tool-failures': tool.state === 'error' }">
            {{ tool.name }} · {{ stateLabels[tool.state] }}
          </summary>
          <pre v-if="tool.input">{{ tool.input }}</pre>
          <pre v-if="tool.output">{{ tool.output }}</pre>
        </details>
      </div>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { LiveTool } from "../types";
import HitlCard from "./HitlCard.vue";

const props = defineProps<{ tools: LiveTool[] }>();
const confirmations = computed(() => props.tools.filter(tool => tool.state === "asking"));
const records = computed(() => props.tools.filter(tool => tool.state !== "asking"));
const failures = computed(() => records.value.filter(tool => tool.state === "error").length);
const running = computed(() => records.value.some(tool =>
  ["pending", "allowed", "submitted", "running"].includes(tool.state),
));
const stateLabels: Record<string, string> = {
  pending: "准备中", asking: "等待确认", allowed: "已允许", submitted: "已提交",
  finished: "调用结束", running: "执行中", success: "已完成", error: "失败",
  interrupted: "已中断", denied: "已拒绝",
};
</script>

<style scoped>
.tool-activity { min-width: 0; display: grid; gap: 6px; }
.tool-records { border: 1px solid #e1ece5; border-radius: 10px; padding: 6px 10px; background: #f7fbf8; font-size: 12px; color: #3f6b56; }
summary { cursor: pointer; }
.tool-running { color: #188b69; }
.tool-failures { color: #b42318; }
.tool-records__body { display: grid; gap: 8px; margin-top: 8px; }
.tool-entry { min-width: 0; }
pre { margin: 6px 0 0; max-height: 240px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
