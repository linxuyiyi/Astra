<template>
  <section class="nebula-home">
    <div class="nebula-stage">
      <NebulaCanvas
        ref="canvasRef"
        :scene="scene"
        :selected-employee-id="selectedEmployeeId"
        :selected-worker-id="selectedWorkerId"
        :selected-task-id="selectedTaskId"
        :selected-collaboration-key="selectedCollaborationKey"
        :show-collaborations="showCollaborations"
        :cruise="cruise"
        :reduced-motion="reduceMotion"
        :pulses="store.pulses"
        :flashes="store.flashes"
        @select="onCanvasSelect"
      />

      <div class="visual-top">
        <div class="eyebrow">
          NEXUS / DIGITAL UNIVERSE
          <span class="live-indicator" :data-state="store.connection">
            {{ store.connection === "live" ? "LIVE" : store.connection === "stale" ? "RECONNECTING" : "CONNECTING" }}
          </span>
        </div>
      </div>

      <NebulaSummary
        :snapshot="store.snapshot"
        :connection="store.connection"
        :last-confirmed-at="store.lastConfirmedAt"
        :refreshing="store.loading"
        :error="store.error"
        @refresh="refreshAll"
      />

      <div class="top-right">
        <el-dropdown
          trigger="click"
          placement="bottom-end"
          popper-class="workbench-dropdown-popper"
          @command="handleNavigate"
          @visible-change="(val: boolean) => (workbenchOpen = val)"
        >
          <button
            type="button"
            class="workbench-link"
            :class="{ pressed: workbenchOpen }"
            title="打开工作台导航"
            aria-label="打开工作台导航"
          >
            ☰ 工作台
          </button>
          <template #dropdown>
            <el-dropdown-menu class="workbench-dropdown-menu">
              <el-dropdown-item
                v-for="item in navItems"
                :key="item.to"
                :command="item.to"
                class="workbench-dropdown-item"
              >
                {{ item.label }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <AttentionEntry
          :snapshot="store.snapshot"
          @select-task="selectTaskFromAttention"
          @select-worker="selectWorker"
        />
        <button
          type="button"
          :aria-pressed="cruise"
          :class="{ pressed: cruise }"
          :disabled="reduceMotion"
          :title="reduceMotion ? '减少动效已开启' : undefined"
          @click="cruise = !cruise"
        >{{ cruise ? "◼ 停止巡航" : "◉ 镜头巡航" }}</button>
        <button type="button" @click="showCollaborations = !showCollaborations">
          {{ showCollaborations ? "隐藏协作连线" : "显示协作连线" }}
        </button>
        <button type="button" @click="resetView">◉ 重置视角</button>
        <button
          type="button"
          :aria-pressed="reduceMotion"
          :class="{ pressed: reduceMotion }"
          @click="toggleMotion"
        >{{ reduceMotion ? "◐ 已减少动效" : "◑ 减少动效" }}</button>
      </div>

      <div class="search">
        <span aria-hidden="true">⌕</span>
        <label for="nebula-employee-focus">定位员工</label>
        <select id="nebula-employee-focus" :value="selectedEmployeeId ?? ''" @change="onFocusSelect">
          <option value="">全部星系</option>
          <option v-for="item in store.employees" :key="item.id" :value="item.id">
            {{ item.name }}{{ item.enabled ? "" : "（已停用）" }}
          </option>
        </select>
      </div>

      <NodeHint
        v-if="hintKind"
        :kind="hintKind"
        :employee="selectedEmployee"
        :worker="selectedWorker"
        :task="selectedTask"
        :collaboration="selectedCollaboration"
        :execution="selectedExecution"
        :employees="store.employees"
        @close="clearSelection"
      />

      <div v-if="!store.snapshot && store.loading" class="stage-state">
        <div class="state-orbit">✦</div>
        <p>正在读取星系快照…</p>
      </div>
      <div v-else-if="!store.snapshot && store.error" class="stage-state error">
        <p>星系快照加载失败：{{ store.error }}</p>
        <button type="button" @click="refreshAll">重新加载</button>
      </div>
      <div v-else-if="store.snapshot && !store.snapshot.employees.length" class="stage-state">
        <div class="state-orbit">✦</div>
        <p>暂无数字员工</p>
        <RouterLink class="state-link" to="/settings/employees">打开员工配置</RouterLink>
      </div>
      <div v-if="store.snapshot && store.error" class="stage-notice">
        {{ store.error }} · 当前显示最近一次已确认状态
        <button type="button" @click="refreshAll">重试</button>
      </div>

      <div class="guide">
        <div class="legend">
          <span style="--c: #58ebbb">执行光路</span>
          <span style="--c: #f5bd69">排队任务</span>
          <span style="--c: #ff946f">人工阻塞</span>
          <span style="--c: #7faccf">跨星系职责请求</span>
          <span style="--c: #fb6484">执行异常</span>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import NebulaCanvas from "../features/nebula/components/NebulaCanvas.vue";
import NebulaSummary from "../features/nebula/components/NebulaSummary.vue";
import AttentionEntry from "../features/nebula/components/AttentionEntry.vue";
import NodeHint, { type NodeHintKind } from "../features/nebula/components/NodeHint.vue";
import { buildScene, collaborationKey, type NebulaScene } from "../features/nebula/layout";
import { useNebulaStore } from "../features/nebula/stores/nebulaStore";
import { getVisibleNavItems } from "../navigation";
import type { NebulaTask } from "../features/nebula/types";
import type { NebulaSelection } from "../features/nebula/renderer";

const STORAGE_KEY = "nexus_nebula_selection";
const MOTION_KEY = "nexus_nebula_reduce_motion";

const store = useNebulaStore();
const route = useRoute();
const router = useRouter();
const canvasRef = ref<InstanceType<typeof NebulaCanvas> | null>(null);

const navItems = computed(() => getVisibleNavItems(route.path));
const workbenchOpen = ref(false);

function handleNavigate(to: string): void {
  workbenchOpen.value = false;
  if (route.path !== to) {
    void router.push(to);
  }
}

const selectedEmployeeId = ref<string | null>(null);
const selectedWorkerId = ref<string | null>(null);
const selectedTaskId = ref<string | null>(null);
const selectedCollaborationKey = ref<string | null>(null);
const selectedExecutionKey = ref<string | null>(null);

const showCollaborations = ref(true);
const cruise = ref(false);
const reduceMotion = ref(false);
let motionMedia: MediaQueryList | null = null;
let motionTouched = false;
let hydrated = false;

const scene = computed<NebulaScene | null>(() =>
  store.snapshot ? buildScene(store.snapshot) : null,
);
const selectedEmployee = computed(() =>
  selectedEmployeeId.value ? store.employeeById.get(selectedEmployeeId.value) ?? null : null,
);
const selectedWorker = computed(() =>
  selectedWorkerId.value ? store.workerById.get(selectedWorkerId.value) ?? null : null,
);
const selectedTask = computed<NebulaTask | null>(() =>
  selectedTaskId.value ? taskFromSnapshot(selectedTaskId.value) : null,
);
const selectedCollaboration = computed(() => {
  const key = selectedCollaborationKey.value;
  if (!key || !store.snapshot) return null;
  return (
    store.snapshot.collaborationEdges.find(
      (edge) => collaborationKey(edge.fromEmployeeId, edge.toEmployeeId) === key,
    ) ?? null
  );
});
const selectedExecution = computed(() => {
  const key = selectedExecutionKey.value;
  if (!key) return null;
  return scene.value?.executionEdges.find((edge) => edge.key === key) ?? null;
});

const hintKind = computed<NodeHintKind | null>(() => {
  if (selectedExecution.value) return "execution";
  if (selectedTask.value) return "task";
  if (selectedWorker.value) return "worker";
  if (selectedCollaboration.value) return "collaboration";
  if (selectedEmployee.value) return "employee";
  return null;
});

function taskFromSnapshot(id: string): NebulaTask | null {
  const snapshot = store.snapshot;
  if (!snapshot) return null;
  return (
    snapshot.queuedTasks.find((item) => item.id === id) ??
    snapshot.attentionTasks.find((item) => item.id === id) ??
    snapshot.unassignedTasks.find((item) => item.id === id) ??
    null
  );
}

function clearSelection(): void {
  selectedEmployeeId.value = null;
  selectedWorkerId.value = null;
  selectedTaskId.value = null;
  selectedCollaborationKey.value = null;
  selectedExecutionKey.value = null;
}

function selectEmployee(id: string): void {
  if (!store.employeeById.get(id)) return;
  const reselect = selectedEmployeeId.value === id;
  selectedEmployeeId.value = id;
  selectedWorkerId.value = null;
  selectedTaskId.value = null;
  selectedCollaborationKey.value = null;
  selectedExecutionKey.value = null;
  if (reselect) canvasRef.value?.focusSelected();
}

function selectWorker(id: string): void {
  const worker = store.workerById.get(id);
  if (!worker) return;
  selectedWorkerId.value = id;
  if (worker.digitalEmployeeId) selectedEmployeeId.value = worker.digitalEmployeeId;
  selectedTaskId.value = null;
  selectedCollaborationKey.value = null;
  selectedExecutionKey.value = null;
}

function selectTask(task: NebulaTask): void {
  selectedTaskId.value = task.id;
  if (task.ownerId) selectedEmployeeId.value = task.ownerId;
  selectedWorkerId.value = null;
  selectedCollaborationKey.value = null;
  selectedExecutionKey.value = null;
}

function selectTaskFromAttention(task: NebulaTask): void {
  selectTask(task);
}

function selectCollaboration(key: string): void {
  const edge = store.snapshot?.collaborationEdges.find(
    (item) => collaborationKey(item.fromEmployeeId, item.toEmployeeId) === key,
  );
  if (!edge) return;
  selectedCollaborationKey.value = key;
  selectedEmployeeId.value = edge.fromEmployeeId;
  selectedWorkerId.value = null;
  selectedTaskId.value = null;
  selectedExecutionKey.value = null;
}

function selectExecution(key: string): void {
  const edge = scene.value?.executionEdges.find((item) => item.key === key);
  if (!edge) return;
  selectedExecutionKey.value = key;
  selectedEmployeeId.value = edge.employeeId;
  selectedWorkerId.value = edge.workerId;
  selectedTaskId.value = null;
  selectedCollaborationKey.value = null;
}

function onCanvasSelect(selection: NebulaSelection): void {
  if (selection.kind === "empty") {
    clearSelection();
    return;
  }
  if (selection.kind === "employee") selectEmployee(selection.id);
  else if (selection.kind === "worker") selectWorker(selection.id);
  else if (selection.kind === "task") {
    const task = taskFromSnapshot(selection.id);
    if (task) selectTask(task);
  } else if (selection.kind === "execution") selectExecution(selection.id);
  else if (selection.kind === "collaboration") selectCollaboration(selection.id);
}

function onFocusSelect(event: Event): void {
  const id = (event.target as HTMLSelectElement).value;
  if (id) selectEmployee(id);
  else clearSelection();
}

function resetView(): void {
  canvasRef.value?.resetView();
}

function toggleMotion(): void {
  reduceMotion.value = !reduceMotion.value;
  motionTouched = true;
  localStorage.setItem(MOTION_KEY, reduceMotion.value ? "1" : "0");
  if (reduceMotion.value) cruise.value = false;
}

async function refreshAll(): Promise<void> {
  await store.refresh();
}

function readStoredSelection(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, string>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function restoreSelection(): void {
  const query = route.query;
  const stored = readStoredSelection();
  selectedEmployeeId.value =
    (typeof query.employee === "string" ? query.employee : stored.employee) || null;
  selectedWorkerId.value =
    (typeof query.worker === "string" ? query.worker : stored.worker) || null;
  selectedTaskId.value =
    (typeof query.task === "string" ? query.task : stored.task) || null;
  selectedCollaborationKey.value =
    (typeof query.collab === "string" ? query.collab : stored.collab) || null;
  selectedExecutionKey.value = null;
}

function syncRoute(): void {
  const query: Record<string, string> = {};
  if (selectedEmployeeId.value) query.employee = selectedEmployeeId.value;
  if (selectedWorkerId.value) query.worker = selectedWorkerId.value;
  if (selectedTaskId.value) query.task = selectedTaskId.value;
  if (selectedCollaborationKey.value) query.collab = selectedCollaborationKey.value;
  const current = route.query;
  const same =
    Object.keys(query).length === Object.keys(current).length &&
    Object.entries(query).every(([key, value]) => current[key] === value);
  if (same) return;
  void router.replace({ path: "/", query });
}

watch(
  [selectedEmployeeId, selectedWorkerId, selectedTaskId, selectedCollaborationKey],
  () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        employee: selectedEmployeeId.value ?? "",
        worker: selectedWorkerId.value ?? "",
        task: selectedTaskId.value ?? "",
        collab: selectedCollaborationKey.value ?? "",
      }),
    );
    if (hydrated) syncRoute();
  },
);

watch(
  () => store.snapshot,
  (snapshot) => {
    if (!snapshot) return;
    if (selectedEmployeeId.value && !snapshot.employees.some((item) => item.id === selectedEmployeeId.value)) {
      selectedEmployeeId.value = null;
      selectedWorkerId.value = null;
      selectedTaskId.value = null;
    }
    if (selectedWorkerId.value && !snapshot.workers.some((item) => item.id === selectedWorkerId.value)) {
      selectedWorkerId.value = null;
    }
    if (selectedTaskId.value && !taskFromSnapshot(selectedTaskId.value)) {
      selectedTaskId.value = null;
    }
    if (selectedCollaborationKey.value) {
      const stillThere = snapshot.collaborationEdges.some(
        (edge) =>
          collaborationKey(edge.fromEmployeeId, edge.toEmployeeId) === selectedCollaborationKey.value,
      );
      if (!stillThere) selectedCollaborationKey.value = null;
    }
    if (!hydrated) hydrated = true;
  },
  { immediate: true },
);

function onMotionPreference(): void {
  if (motionTouched) return;
  reduceMotion.value = motionMedia?.matches ?? false;
}

onMounted(() => {
  const stored = localStorage.getItem(MOTION_KEY);
  motionMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (stored === "1" || stored === "0") {
    motionTouched = true;
    reduceMotion.value = stored === "1";
  } else {
    reduceMotion.value = motionMedia.matches;
  }
  motionMedia.addEventListener("change", onMotionPreference);
  restoreSelection();
  store.start();
});

onBeforeUnmount(() => {
  store.stop();
  motionMedia?.removeEventListener("change", onMotionPreference);
});
</script>

<style scoped>
.nebula-home {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: #010e16;
}
.nebula-stage {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: radial-gradient(ellipse at 49% 43%, #0b3c34 0%, #052923 46%, #031a1a 90%);
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.visual-top { position: absolute; top: 22px; left: 26px; pointer-events: none; z-index: 3; max-width: calc(100% - 600px); }
.eyebrow { font-size: 9px; font-weight: 750; letter-spacing: 2.4px; color: #67cba8; }
.live-indicator {
  display: inline-block; margin-left: 10px; border-radius: 12px; background: #0b543c;
  border: 1px solid #1e9874; padding: 3px 7px; font-size: 8px; letter-spacing: 1px;
  color: #8fffd5; box-shadow: 0 0 14px #1fa67c30;
}
.live-indicator[data-state="stale"] { border-color: #a27a37; color: #ffd39a; background: #4a3512; }
.live-indicator[data-state="offline"] { border-color: #a4545e; color: #ffb0bb; background: #4b1b22; }
.live-indicator[data-state="connecting"] { color: #9dbab0; border-color: #3d6459; background: #123b33; }
.top-right {
  position: absolute; right: 22px; top: 20px; display: flex; align-items: center; gap: 6px;
  flex-wrap: wrap; justify-content: flex-end; z-index: 8; max-width: calc(100% - 40px);
}
.top-right button, .workbench-link {
  background: #103f38df; color: #b7e2d3; border: 1px solid #366a5d; border-radius: 9px;
  padding: 7px 10px; font-size: 10px; cursor: pointer; white-space: nowrap; text-decoration: none;
}
.top-right button:hover, .workbench-link:hover { background: #155246; }
.top-right button.pressed { background: #128e6f; color: #e2fff4; border-color: #5de6bb; box-shadow: 0 0 18px #2fc39455; }
.top-right button:disabled { opacity: .5; cursor: not-allowed; }
.workbench-link {
  background: #0d483c;
  color: #d5f4e9;
  border-color: #347b67;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: inherit;
}
.workbench-link:focus,
.workbench-link:focus-visible {
  outline: none;
}
:global(.workbench-dropdown-popper) {
  --el-bg-color-overlay: #072a23;
  --el-border-color-light: #347b67;
  --el-text-color-regular: #d5f4e9;
  --el-text-color-primary: #e6fff4;
  --el-dropdown-menuItem-hover-fill: #155246;
  --el-dropdown-menuItem-hover-color: #8fffd5;
}
:global(.workbench-dropdown-popper .el-dropdown-menu) {
  background: #072a23;
  border: 1px solid #347b67;
  border-radius: 10px;
  padding: 5px;
  min-width: 130px;
  box-shadow: 0 14px 32px rgba(0, 26, 26, 0.7);
  backdrop-filter: blur(12px);
}
:global(.workbench-dropdown-popper .el-dropdown-menu__item) {
  color: #d5f4e9;
  font-size: 11px;
  padding: 8px 14px;
  border-radius: 6px;
  letter-spacing: 0.3px;
  cursor: pointer;
}
:global(.workbench-dropdown-popper .el-dropdown-menu__item:hover),
:global(.workbench-dropdown-popper .el-dropdown-menu__item:focus) {
  background-color: #155246;
  color: #8fffd5;
}
:global(.workbench-dropdown-popper .el-popper__arrow::before) {
  background: #072a23 !important;
  border-color: #347b67 !important;
}
.search {
  position: absolute; right: 22px; top: 64px; z-index: 4; display: flex; align-items: center; gap: 6px;
  background: #0a4036c9; border: 1px solid #316a58; border-radius: 9px; padding: 6px 9px;
  color: #7fd0b3; font-size: 11px; backdrop-filter: blur(10px);
}
.search label { color: #9fcfbc; }
.search select { background: transparent; color: #d7fff0; border: 0; outline: none; max-width: 156px; font-size: 11px; }
.search option { color: #12372e; background: white; }
.guide {
  position: absolute; left: 26px; bottom: 18px; padding: 12px 15px; border: 1px solid #ffffff21;
  border-radius: 13px; background: #052920d9; backdrop-filter: blur(12px);
  max-width: calc(100% - 420px); box-shadow: 0 14px 30px #001a1a44; z-index: 3;
}
.legend { display: flex; align-items: center; flex-wrap: wrap; gap: 13px; margin-top: 9px; color: #91baa9; font-size: 9px; }
.legend span::before {
  content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--c); margin-right: 5px; vertical-align: 1px;
}
.stage-state {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
  z-index: 15; text-align: center; color: #b7e2d3; font-size: 12px; line-height: 1.8;
  background: #052920d9; border: 1px solid #ffffff21; border-radius: 14px;
  padding: 22px 26px; max-width: 420px; backdrop-filter: blur(12px);
}
.stage-state.error { border-color: #a4545e; color: #ffb9c3; }
.stage-state button { margin-top: 8px; border: 1px solid #347b67; background: #0d483c; color: #d5f4e9; border-radius: 7px; padding: 6px 10px; font-size: 11px; cursor: pointer; }
.state-orbit { font-size: 30px; color: #4fe0b3; margin-bottom: 6px; }
.state-link { display: inline-flex; margin-top: 10px; color: #8fffd5; font-size: 11px; }
.stage-notice {
  position: absolute; left: 50%; top: 16px; transform: translateX(-50%);
  z-index: 16; background: #4a3512e8; border: 1px solid #a27a37; color: #ffe0b8;
  border-radius: 9px; padding: 7px 11px; font-size: 10px; display: flex; gap: 8px; align-items: center;
  max-width: min(620px, calc(100% - 40px)); line-height: 1.5;
}
.stage-notice button { border: 0; background: #79531d; color: #fff0d2; border-radius: 5px; font-size: 9px; padding: 3px 7px; cursor: pointer; }
@media (max-width: 1500px) {
  .visual-top { max-width: calc(100% - 560px); }
  .guide { max-width: calc(100% - 390px); }
}
@media (max-width: 1180px) {
  .visual-top { display: none; }
  .top-right { max-width: 620px; }
  .guide { max-width: calc(100% - 360px); }
}
@media (max-width: 900px) {
  .nebula-home { position: fixed; inset: 0; }
  .top-right { right: 12px; top: 12px; max-width: calc(100% - 24px); }
  .search { right: 12px; top: 58px; }
  .guide { left: 12px; bottom: 12px; max-width: calc(100% - 24px); }
  .legend { gap: 9px; }
}
</style>
