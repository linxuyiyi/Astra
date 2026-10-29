<template>
  <Transition name="node-hint">
    <aside
      v-if="kind"
      class="node-hint"
      role="region"
      aria-label="节点提示"
      @click.stop
    >
      <button type="button" class="hint-close" aria-label="关闭节点提示" @click="emit('close')">×</button>
      <div class="hint-kind">{{ kindLabel }}</div>

      <div class="hint-head">
        <EmployeeAvatar v-if="kind === 'employee' && employee" :avatar="employee.avatar" :size="44" />
        <span v-else class="hint-avatar">{{ avatar }}</span>
        <div class="hint-title">
          <h3>{{ title }}</h3>
          <small v-if="subtitle">{{ subtitle }}</small>
        </div>
        <span v-if="badge" class="hint-badge" :data-tone="tone">{{ badge }}</span>
      </div>

      <p v-if="description" class="hint-desc">{{ description }}</p>

      <div v-if="kind === 'employee' && employee" class="hint-metrics">
        <span><b>{{ employee.counts.running }}</b>执行</span>
        <span><b>{{ employee.counts.queued }}</b>排队</span>
        <span><b>{{ employee.counts.waiting }}</b>待人工</span>
        <span><b>{{ employee.counts.failed }}</b>异常</span>
        <span><b>{{ employee.counts.completedToday }}</b>今日完成</span>
      </div>

      <dl v-if="kind === 'worker' && worker" class="hint-facts">
        <div><dt>健康</dt><dd>{{ healthText(worker.health) }}</dd></div>
        <div><dt>占用</dt><dd>{{ occupancyText(worker.occupancy) }}</dd></div>
        <div><dt>关联任务</dt><dd>{{ worker.taskCount }} 项</dd></div>
        <div><dt>执行环境</dt><dd>{{ worker.environmentName || "—" }}</dd></div>
        <div><dt>最近心跳</dt><dd>{{ formatTime(worker.lastHeartbeatAt) }}</dd></div>
      </dl>

      <dl v-if="kind === 'execution' && execution" class="hint-facts">
        <div><dt>Worker</dt><dd>{{ worker?.name || "—" }}</dd></div>
        <div><dt>执行中</dt><dd>{{ execution.running }} 项</dd></div>
        <div><dt>待人工</dt><dd>{{ execution.waiting }} 项</dd></div>
        <div><dt>状态待确认</dt><dd>{{ execution.unavailable }} 项</dd></div>
      </dl>

      <dl v-if="kind === 'task' && task" class="hint-facts">
        <div><dt>任务状态</dt><dd>{{ statusText(task.status) }}</dd></div>
        <div><dt>执行 Worker</dt><dd>{{ task.workerName || "尚未绑定" }}</dd></div>
        <div><dt>责任员工</dt><dd>{{ task.ownerName || "未关联" }}</dd></div>
        <div><dt>更新时间</dt><dd>{{ formatTime(task.updatedAt) }}</dd></div>
      </dl>

      <p v-if="kind === 'task' && task?.waitingReason" class="hint-reason wait">
        {{ task.waitingReason }}
      </p>
      <p v-if="kind === 'task' && task?.failureReason" class="hint-reason fail">
        {{ task.failureReason }}
      </p>
      <p v-if="kind === 'task' && task?.conflict" class="hint-reason fail">
        责任员工与执行 Worker 归属冲突，画布不绘制执行光路。
      </p>

      <div v-if="kind === 'collaboration' && collaboration" class="hint-collab">
        <p>
          <b>{{ collaboration.requestCount }}</b> 次职责请求 · 最近
          {{ formatTime(collaboration.lastRequestedAt) }}
          <span :class="{ live: collaboration.recent }">{{ collaboration.recent ? "24 小时内" : "历史关系" }}</span>
        </p>
        <div v-for="(direction, index) in collaboration.directions" :key="index" class="hint-direction">
          {{ employeeName(direction.fromEmployeeId) }} → {{ employeeName(direction.toEmployeeId) }}
          <span>{{ direction.count }} 次</span>
        </div>
      </div>

      <div class="hint-actions">
        <template v-if="kind === 'employee' && employee">
          <RouterLink class="hint-primary" :to="{ path: '/chat', query: { employee: employee.id } }">
            进入员工对话
          </RouterLink>
          <RouterLink class="hint-link" :to="{ path: '/tasks', query: { owner: employee.id } }">
            查看该员工任务
          </RouterLink>
        </template>

        <template v-else-if="kind === 'worker' && worker">
          <RouterLink class="hint-primary" :to="`/settings/workers/${worker.id}`">
            打开 Worker 详情
          </RouterLink>
          <RouterLink class="hint-link" :to="{ path: '/tasks', query: { employee: worker.id } }">
            查看关联任务
          </RouterLink>
          <RouterLink
            v-if="worker.digitalEmployeeId"
            class="hint-link"
            :to="{ path: '/chat', query: { employee: worker.digitalEmployeeId } }"
          >员工对话</RouterLink>
        </template>

        <template v-else-if="kind === 'execution' && execution">
          <RouterLink class="hint-primary" :to="{ path: '/tasks', query: { employee: execution.workerId } }">
            查看本 Worker 关联任务
          </RouterLink>
          <RouterLink class="hint-link" :to="`/settings/workers/${execution.workerId}`">
            Worker 详情
          </RouterLink>
        </template>

        <template v-else-if="kind === 'task' && task">
          <RouterLink class="hint-primary" :to="`/tasks/${task.id}`">打开任务详情</RouterLink>
          <RouterLink
            v-if="task.ownerId"
            class="hint-link"
            :to="{ path: '/tasks', query: { owner: task.ownerId } }"
          >该员工全部任务</RouterLink>
        </template>

        <template v-else-if="kind === 'collaboration' && collaboration">
          <RouterLink
            class="hint-primary"
            :to="{ path: '/chat', query: { employee: collaboration.fromEmployeeId } }"
          >和 {{ employeeName(collaboration.fromEmployeeId) }} 对话</RouterLink>
          <RouterLink
            class="hint-link"
            :to="{ path: '/chat', query: { employee: collaboration.toEmployeeId } }"
          >和 {{ employeeName(collaboration.toEmployeeId) }} 对话</RouterLink>
        </template>
      </div>
    </aside>
  </Transition>
</template>

<script setup lang="ts">
import { computed } from "vue";
import EmployeeAvatar from "../../../components/EmployeeAvatar.vue";
import { RouterLink } from "vue-router";
import {
  HEALTH_TEXT,
  OCCUPANCY_TEXT,
  STATUS_TEXT,
  formatTime,
  type ExecutionEdgeNode,
} from "../layout";
import type {
  NebulaCollaborationEdge,
  NebulaEmployee,
  NebulaTask,
  NebulaWorker,
} from "../types";

export type NodeHintKind = "employee" | "worker" | "execution" | "task" | "collaboration";

const props = defineProps<{
  kind: NodeHintKind | null;
  employee: NebulaEmployee | null;
  worker: NebulaWorker | null;
  task: NebulaTask | null;
  collaboration: NebulaCollaborationEdge | null;
  execution: ExecutionEdgeNode | null;
  employees: NebulaEmployee[];
}>();

const emit = defineEmits<{ close: [] }>();

const kindLabel = computed(
  () =>
    ({
      employee: "DIGITAL EMPLOYEE / 数字员工",
      worker: "AGENT WORKER / 执行节点",
      execution: "EXECUTION ROUTE / 执行光路",
      task: "TASK / 任务",
      collaboration: "COLLABORATION / 职责协作",
    })[props.kind ?? "employee"],
);

const title = computed(() => {
  if (props.kind === "employee") return props.employee?.name ?? "数字员工";
  if (props.kind === "worker") return props.worker?.name ?? "Agent Worker";
  if (props.kind === "execution") return props.worker?.name ?? "执行光路";
  if (props.kind === "task") return props.task?.title ?? "任务";
  return props.collaboration
    ? `${employeeName(props.collaboration.fromEmployeeId)} ⇄ ${employeeName(props.collaboration.toEmployeeId)}`
    : "员工职责协作";
});

const subtitle = computed(() => {
  if (props.kind === "employee") return props.employee?.domain || "数字员工";
  if (props.kind === "worker") {
    return `${props.worker?.role || "执行节点"} · ${props.worker?.environmentMode || "环境未知"}`;
  }
  if (props.kind === "execution") return "";
  if (props.kind === "task") return `${props.task?.id ?? ""} · ${props.task?.priority ?? ""}`;
  return "";
});

const description = computed(() => {
  if (props.kind === "employee") return props.employee?.description || "";
  if (props.kind === "worker") return props.worker?.capabilityLabels.join(" · ") || "";
  if (props.kind === "execution") return `${props.execution?.running ?? 0} 项执行中，${props.execution?.waiting ?? 0} 项等待人工`;
  if (props.kind === "task") return props.task?.currentProgress || "";
  return "";
});

const avatar = computed(() => {
  if (props.kind === "worker" || props.kind === "execution") return "◈";
  if (props.kind === "task") return "▣";
  return "⇄";
});

const badge = computed(() => {
  if (props.kind === "employee") {
    if (!props.employee) return "";
    if (!props.employee.enabled) return "已停用";
    if (props.employee.counts.waiting) return "待人工";
    if (props.employee.counts.failed) return "异常";
    if (props.employee.counts.running) return "运行中";
    return props.employee.interactionStatus === "available" ? "可交互" : "未绑定对话";
  }
  if (props.kind === "worker" || props.kind === "execution") {
    return healthText(props.worker?.health ?? "unknown");
  }
  if (props.kind === "task") return statusText(props.task?.status ?? "");
  return "历史关系";
});

const tone = computed(() => {
  if (props.kind === "employee") {
    if (!props.employee?.enabled) return "muted";
    if (props.employee.counts.waiting || props.employee.counts.failed) return "warn";
    if (props.employee.counts.running) return "ok";
    return "ok";
  }
  if (props.kind === "worker" || props.kind === "execution") {
    if (props.worker?.health === "error") return "warn";
    if (props.worker?.health === "offline") return "muted";
    return "ok";
  }
  if (props.kind === "task") {
    if (props.task?.status === "FAILED") return "warn";
    if (props.task?.status === "WAITING") return "wait";
    if (props.task?.status === "SUCCEEDED") return "muted";
    return "ok";
  }
  return "ok";
});

function employeeName(id: string): string {
  return props.employees.find((item) => item.id === id)?.name ?? id;
}

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

function healthText(health: string): string {
  return HEALTH_TEXT[health] ?? health;
}

function occupancyText(occupancy: string): string {
  return OCCUPANCY_TEXT[occupancy] ?? occupancy;
}
</script>

<style scoped>
.node-hint {
  position: absolute;
  z-index: 20;
  width: 330px;
  max-width: calc(100% - 24px);
  max-height: min(70vh, 560px);
  overflow: auto;
  padding: 14px 15px 13px;
  border: 1px solid #3a7d6a;
  border-radius: 14px;
  background: #062b25e8;
  color: #dff9ee;
  box-shadow: 0 18px 48px #001a1a80;
  backdrop-filter: blur(16px);
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.node-hint { left: 26px; bottom: 128px; }
.hint-close {
  position: absolute;
  right: 9px;
  top: 7px;
  border: 0;
  background: transparent;
  color: #87b8a7;
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
  padding: 3px 5px;
}
.hint-close:hover { color: #e2fff4; }
.hint-kind { color: #5fd6b0; font-size: 8px; font-weight: 800; letter-spacing: 1.4px; }
.hint-head { display: flex; align-items: center; gap: 9px; margin-top: 7px; }
.hint-avatar {
  width: 34px; height: 34px; flex: none; border-radius: 10px;
  display: grid; place-items: center; font-size: 17px;
  background: linear-gradient(135deg, #1b735d, #0a3c33);
  border: 1px solid #3f8b75;
}
.hint-title { min-width: 0; flex: 1; }
.hint-title h3 { margin: 0; font-size: 15px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint-title small { display: block; color: #8dbdab; font-size: 9px; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint-badge {
  border-radius: 20px; padding: 3px 7px; font-size: 9px; white-space: nowrap;
  background: #0d5b47; border: 1px solid #2f9474; color: #a9ffdc;
}
.hint-badge[data-tone="warn"] { background: #5a3415; border-color: #a36b2e; color: #ffd5a0; }
.hint-badge[data-tone="wait"] { background: #5d2f1c; border-color: #b66a3f; color: #ffc49e; }
.hint-badge[data-tone="muted"] { background: #1b3d38; border-color: #44685e; color: #a9c3b9; }
.hint-desc { max-height: 6.8em; overflow-y: auto; overflow-wrap: anywhere; white-space: pre-wrap; margin: 9px 0 0; color: #a4cabb; font-size: 10px; line-height: 1.7; }
.hint-metrics { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; color: #8dbdab; font-size: 9px; }
.hint-metrics b { color: #e6fff4; font-size: 14px; margin-right: 3px; }
.hint-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; margin: 10px 0 0; }
.hint-facts div { min-width: 0; }
.hint-facts dt { color: #82ad9e; font-size: 9px; }
.hint-facts dd { margin: 2px 0 0; color: #d7f4e8; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint-reason { overflow-wrap: anywhere; margin: 8px 0 0; font-size: 10px; line-height: 1.6; }
.hint-reason.wait { color: #ffc89e; }
.hint-reason.fail { color: #ff9eaf; }
.hint-collab { margin-top: 9px; border-top: 1px dashed #3e7365; padding-top: 7px; }
.hint-collab p { margin: 0 0 5px; color: #a4cabb; font-size: 10px; }
.hint-collab p b { color: #e6fff4; }
.hint-collab .live { color: #6ff0bf; }
.hint-direction { display: flex; justify-content: space-between; gap: 8px; color: #bfe8d9; font-size: 10px; padding: 2px 0; }
.hint-direction span { color: #81b09f; }
.hint-actions { overflow-wrap: anywhere; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 11px; }
.hint-primary, .hint-link {
  display: inline-flex; align-items: center; min-height: 27px; padding: 0 9px;
  border-radius: 7px; font-size: 10px; text-decoration: none; white-space: nowrap;
}
.hint-primary { background: #0f9c75; color: #fff; }
.hint-primary:hover { background: #0b8764; }
.hint-link { border: 1px solid #3d806c; color: #a8e5cf; background: #0a3b32; }
.hint-link:hover { border-color: #60c9a3; color: #e6fff4; }
.node-hint-enter-active, .node-hint-leave-active { transition: opacity .16s ease, transform .16s ease; }
.node-hint-enter-from, .node-hint-leave-to { opacity: 0; transform: translateY(5px); }
@media (max-width: 900px) {
  .node-hint { left: 12px; bottom: 78px; width: calc(100% - 24px); }
}
</style>
