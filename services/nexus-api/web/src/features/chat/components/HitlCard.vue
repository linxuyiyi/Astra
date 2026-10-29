<template>
  <div class="hitl-card" role="group" aria-label="需要确认的操作">
    <div class="hitl-card__head">
      <strong>{{ tool.name || "工具操作" }}</strong>
      <span class="muted">等待选择</span>
    </div>
    <pre v-if="tool.input" class="hitl-card__body">{{ tool.input }}</pre>
    <p class="hitl-card__prompt">是否允许本次操作？</p>
    <div class="hitl-card__options">
      <button
        v-for="option in options"
        :key="option.value"
        type="button"
        class="hitl-card__option"
        :class="{ sel: selected === option.value }"
        :disabled="done"
        @mouseenter="selected = option.value"
        @click="choose(option.value)"
      >
        <span class="hitl-card__marker">{{ selected === option.value ? "›" : "" }}</span>
        {{ option.label }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import type { LiveTool } from "../types";

const props = defineProps<{ tool: LiveTool }>();
const emit = defineEmits<{ confirm: [choice: "yes" | "yes_with_rule" | "no"] }>();

const options = [
  { value: "yes" as const, label: "1. 允许本次" },
  { value: "yes_with_rule" as const, label: "2. 允许并记住" },
  { value: "no" as const, label: "3. 拒绝" },
];
const selected = ref<"yes" | "yes_with_rule" | "no">("yes");
const done = ref(false);

function choose(value: "yes" | "yes_with_rule" | "no") {
  if (done.value) return;
  selected.value = value;
  done.value = true;
  // 只接线展示：把选择抛给上层；PR#12 / AgentWork 机制不在此改动。
  emit("confirm", value);
}
</script>

<style scoped>
.hitl-card {
  margin-top: 8px;
  padding: 14px 16px;
  border-radius: 14px;
  border: 2px solid var(--green, #188b69);
  background: #ffffff;
  display: grid;
  gap: 10px;
  box-shadow: 0 1px 2px rgba(24, 139, 105, 0.06), 0 8px 22px rgba(24, 139, 105, 0.08);
}
.hitl-card__head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.hitl-card__head strong { color: var(--ink, #203830); font-size: 13px; }
.hitl-card__body {
  margin: 0;
  max-height: 160px;
  overflow: auto;
  padding: 8px 10px;
  border-radius: 8px;
  background: #f7fbf8;
  border: 1px solid #e4efe8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  line-height: 1.6;
}
.hitl-card__prompt { margin: 0; font-size: 12px; color: #187956; font-weight: 700; }
.hitl-card__options { display: grid; gap: 6px; }
.hitl-card__option {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid #d7ebe0;
  background: #fbfefc;
  text-align: left;
  padding: 10px 12px;
  border-radius: 10px;
  color: #3f6b56;
  font-weight: 650;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease, color 0.12s ease;
}
.hitl-card__option:hover:not(:disabled) {
  border-color: #a8d4c0;
  background: #eef8f2;
}
.hitl-card__option.sel {
  color: #117154;
  background: #e5f6ed;
  border-color: var(--green, #188b69);
  box-shadow: inset 0 0 0 1px rgba(24, 139, 105, 0.18);
}
.hitl-card__option:disabled { opacity: 0.65; cursor: default; }
.hitl-card__marker { width: 14px; display: inline-block; color: var(--green, #188b69); font-weight: 800; }
</style>
