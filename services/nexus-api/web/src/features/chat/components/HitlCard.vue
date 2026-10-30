<template>
  <div class="hitl-card" role="status" aria-label="等待确认的操作">
    <strong>{{ tool.name || "工具操作" }} · 等待确认</strong>
    <pre v-if="tool.input">{{ tool.input }}</pre>
    <p>当前窗口尚未接入确认处理，操作仍在等待确认。</p>
  </div>
</template>
<script setup lang="ts">
import type { LiveTool } from "../types";
defineProps<{ tool: LiveTool }>();
</script>
<style scoped>
.hitl-card { margin-top: 8px; padding: 14px 16px; border: 1px solid #c4daca; border-radius: 10px; background: #f7fbf8; }
.hitl-card strong { color: #3f6b56; font-size: 13px; }
.hitl-card pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 160px; overflow: auto; font-size: 12px; }
.hitl-card p { font-size: 12px; color: #6f8268; }
</style>
