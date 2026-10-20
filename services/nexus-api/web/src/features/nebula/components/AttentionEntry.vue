<template>
  <el-popover
    v-model:visible="open"
    placement="bottom-end"
    :width="370"
    trigger="click"
    :disabled="count === 0 && !unhealthy.length"
  >
    <template #reference>
      <button type="button" class="attention-trigger" :class="{ alert: count > 0 }">
        ◉ 待处理
        <b v-if="count > 0">{{ count }}</b>
      </button>
    </template>
    <div class="attention-panel">
      <header>
        <b>需要关注</b>
        <span>{{ count }} 个任务 · {{ unhealthy.length }} 台 Worker</span>
      </header>
      <div v-if="tasks.length" class="attention-block">
        <div v-for="item in tasks" :key="item.id" class="attention-item">
          <span class="status-pill" :class="pillClass(item.status)">{{ statusText(item.status) }}</span>
          <button type="button" @click="selectTask(item)">
            <b>{{ item.title }}</b>
            <small>{{ item.ownerName || "未关联员工" }} · {{ item.waitingReason || item.failureReason || formatTime(item.updatedAt) }}</small>
          </button>
        </div>
      </div>
      <div v-else class="attention-empty">没有等待人工、失败或连接异常的任务。</div>
      <div v-if="unhealthy.length" class="attention-block">
        <div v-for="item in unhealthy" :key="item.id" class="attention-item">
          <span class="status-pill" :class="item.health === 'error' ? 'fail' : 'muted'">{{ healthText(item.health) }}</span>
          <button type="button" @click="selectWorker(item.id)">
            <b>{{ item.name }}</b>
            <small>{{ employeeName(item.digitalEmployeeId) }} · {{ item.taskCount }} 项关联任务</small>
          </button>
        </div>
      </div>
      <RouterLink class="attention-link" :to="{ path: '/tasks', query: { view: 'interventions' } }">
        打开任务中心待处理视图
      </RouterLink>
    </div>
  </el-popover>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";
import { HEALTH_TEXT, STATUS_TEXT, formatTime } from "../layout";
import type { NebulaSnapshot, NebulaTask } from "../types";

const props = defineProps<{ snapshot: NebulaSnapshot | null }>();
const emit = defineEmits<{
  "select-task": [task: NebulaTask];
  "select-worker": [workerId: string];
}>();

const open = ref(false);
const tasks = computed(() => props.snapshot?.attentionTasks ?? []);
const count = computed(() => tasks.value.length);
const unhealthy = computed(() =>
  (props.snapshot?.workers ?? []).filter((item) => ["error", "offline"].includes(item.health)),
);

function employeeName(id: string | null): string {
  if (!id) return "未关联员工";
  return props.snapshot?.employees.find((item) => item.id === id)?.name ?? id;
}

function selectTask(task: NebulaTask): void {
  emit("select-task", task);
  open.value = false;
}

function selectWorker(workerId: string): void {
  emit("select-worker", workerId);
  open.value = false;
}

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

function healthText(health: string): string {
  return HEALTH_TEXT[health] ?? health;
}

function pillClass(status: string): string {
  if (status === "PENDING") return "queue";
  if (status === "WAITING") return "wait";
  if (status === "FAILED") return "fail";
  if (status === "SUCCEEDED") return "done";
  return "run";
}
</script>

<style scoped>
.attention-trigger {
  background: #103f38df;
  color: #b7e2d3;
  border: 1px solid #366a5d;
  border-radius: 9px;
  padding: 8px 12px;
  font-size: 11px;
  cursor: pointer;
}
.attention-trigger:hover { background: #145246; }
.attention-trigger.alert { color: #ffe0b8; border-color: #9a6d34; background: #463012e6; }
.attention-trigger b {
  display: inline-grid;
  place-items: center;
  min-width: 16px;
  height: 16px;
  margin-left: 4px;
  border-radius: 20px;
  background: #d98242;
  color: #fff;
  font-size: 9px;
}
.attention-panel { display: flex; flex-direction: column; gap: 8px; max-height: 430px; overflow: auto; }
.attention-panel header { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.attention-panel header b { font-size: 13px; color: #1b3b35; }
.attention-panel header span { font-size: 10px; color: #8ba196; }
.attention-block { display: grid; gap: 5px; }
.attention-item {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  border: 1px solid #e2efe8;
  border-radius: 8px;
  padding: 7px 8px;
  background: #fbfefc;
}
.attention-item button { border: 0; background: transparent; text-align: left; cursor: pointer; display: grid; gap: 2px; min-width: 0; flex: 1; padding: 0; }
.attention-item b { font-size: 11px; color: #1b3b35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attention-item small { font-size: 9px; color: #8ba196; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-pill { border-radius: 20px; background: #e4f7ed; color: #07946c; padding: 3px 6px; font-size: 9px; white-space: nowrap; }
.status-pill.queue { background: #fff5e5; color: #b47f27; }
.status-pill.wait { background: #ffecdf; color: #c76d39; }
.status-pill.fail { background: #fde9ec; color: #bd4e60; }
.status-pill.done { background: #edf3fa; color: #6f93ad; }
.status-pill.muted { background: #eef2f1; color: #7d918a; }
.attention-empty { padding: 12px; border: 1px dashed #d0e4db; border-radius: 8px; color: #9bafa5; font-size: 11px; text-align: center; }
.attention-link { color: #0b9e7b; font-size: 11px; text-decoration: none; align-self: flex-start; }
.attention-link:hover { text-decoration: underline; }
</style>
