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
        <h1>每个数字员工，都是一座运行中的星系</h1>
        <p>员工是意识核心 · 专属 Worker 沿轨运行 · 任务在光路中流转</p>
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
        <AttentionEntry
          :snapshot="store.snapshot"
          @select-task="selectTask"
          @select-worker="selectWorker"
        />
        <button
          type="button"
          :aria-pressed="cruise"
          :class="{ pressed: cruise }"
          :disabled="reduceMotion"
          :title="reduceMotion ? '减少动效已开启' : '环绕当前视野缓慢巡航'"
          @click="cruise = !cruise"
        >
          {{ cruise ? "◼ 停止巡航" : "◉ 镜头巡航" }}
        </button>
        <button type="button" @click="showCollaborations = !showCollaborations">
          {{ showCollaborations ? "隐藏协作连线" : "显示协作连线" }}
        </button>
        <button type="button" @click="resetView">◉ 重置视角</button>
        <button type="button" :aria-pressed="reduceMotion" :class="{ pressed: reduceMotion }" @click="toggleMotion">
          {{ reduceMotion ? "◐ 已减少动效" : "◑ 减少动效" }}
        </button>
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
        <p>当前还没有数字员工。先去配置中心创建员工，星系会按目录自动生成。</p>
        <RouterLink class="state-link" to="/settings/employees">打开员工配置</RouterLink>
      </div>
      <div v-if="store.snapshot && store.error" class="stage-notice">
        {{ store.error }} · 当前显示最近一次已确认状态
        <button type="button" @click="refreshAll">重试</button>
      </div>

      <div class="guide">
        <div class="guide-title">
          读法
          <small>每个节点、连线和数量都来自 Nexus 正式数据；画布不承担调度，也不模拟执行。</small>
        </div>
        <div class="legend">
          <span style="--c: #58ebbb">执行光路</span>
          <span style="--c: #f5bd69">排队任务</span>
          <span style="--c: #ff946f">人工阻塞</span>
          <span style="--c: #7faccf">跨星系职责请求</span>
          <span style="--c: #fb6484">执行异常</span>
          <span style="--c: #d8e8e1">Worker 健康另行展示</span>
        </div>
      </div>
      <div class="tip">
        三维透视 · 拖动环视 · 滚轮远近 · Shift+拖动平移 · 点击节点查看业务事实
      </div>
    </div>

    <GalaxyInspector
      v-model:tab="tab"
      :snapshot="store.snapshot"
      :employee="selectedEmployee"
      :worker="selectedWorker"
      :task="selectedTask"
      :collaboration="selectedCollaboration"
      @select="onInspectorSelect"
      @refresh="refreshAll"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import NebulaCanvas from "../features/nebula/components/NebulaCanvas.vue";
import NebulaSummary from "../features/nebula/components/NebulaSummary.vue";
import AttentionEntry from "../features/nebula/components/AttentionEntry.vue";
import GalaxyInspector, { type InspectorTab } from "../features/nebula/components/GalaxyInspector.vue";
import { getNebulaTask } from "../features/nebula/api";
import { buildScene, collaborationKey, type NebulaScene } from "../features/nebula/layout";
import { useNebulaStore } from "../features/nebula/stores/nebulaStore";
import type { NebulaEmployee, NebulaTask } from "../features/nebula/types";
import type { NebulaSelection } from "../features/nebula/renderer";

const STORAGE_KEY = "nexus_nebula_selection";
const MOTION_KEY = "nexus_nebula_reduce_motion";

const store = useNebulaStore();
const route = useRoute();
const router = useRouter();
const canvasRef = ref<InstanceType<typeof NebulaCanvas> | null>(null);

const selectedEmployeeId = ref<string | null>(null);
const selectedWorkerId = ref<string | null>(null);
const selectedTaskId = ref<string | null>(null);
const selectedTask = ref<NebulaTask | null>(null);
const selectedCollaborationKey = ref<string | null>(null);
const tab = ref<InspectorTab>("overview");
const showCollaborations = ref(true);
const cruise = ref(false);
const reduceMotion = ref(false);
let motionMedia: MediaQueryList | null = null;
let motionTouched = false;
let hydrated = false;
let taskRequestGeneration = 0;

const scene = computed<NebulaScene | null>(() =>
  store.snapshot ? buildScene(store.snapshot) : null,
);
const selectedEmployee = computed<NebulaEmployee | null>(() =>
  selectedEmployeeId.value ? store.employeeById.get(selectedEmployeeId.value) ?? null : null,
);
const selectedWorker = computed(() =>
  selectedWorkerId.value ? store.workerById.get(selectedWorkerId.value) ?? null : null,
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

async function ensureSelectedTask(): Promise<void> {
  const id = selectedTaskId.value;
  if (!id) {
    selectedTask.value = null;
    return;
  }
  const cached = taskFromSnapshot(id);
  if (cached) {
    selectedTask.value = cached;
    return;
  }
  const generation = ++taskRequestGeneration;
  try {
    const response = await getNebulaTask(id);
    if (generation !== taskRequestGeneration) return;
    selectedTask.value = response.task;
    if (response.task.ownerId) selectedEmployeeId.value = response.task.ownerId;
  } catch {
    if (generation === taskRequestGeneration) selectedTask.value = null;
  }
}

function selectEmployee(id: string): void {
  const employee = store.employeeById.get(id);
  if (!employee) return;
  const reselect = selectedEmployeeId.value === id;
  selectedEmployeeId.value = id;
  selectedWorkerId.value = null;
  selectedTaskId.value = null;
  selectedTask.value = null;
  selectedCollaborationKey.value = null;
  tab.value = "chat";
  if (reselect) canvasRef.value?.focusSelected();
}

function selectWorker(id: string): void {
  const worker = store.workerById.get(id);
  if (!worker) return;
  if (!worker.digitalEmployeeId) {
    void router.push(`/settings/workers/${id}`);
    return;
  }
  selectedEmployeeId.value = worker.digitalEmployeeId;
  selectedWorkerId.value = id;
  selectedTaskId.value = null;
  selectedTask.value = null;
  selectedCollaborationKey.value = null;
  tab.value = "tasks";
}

function selectTask(task: NebulaTask): void {
  selectedTaskId.value = task.id;
  selectedTask.value = task;
  if (task.ownerId) selectedEmployeeId.value = task.ownerId;
  selectedWorkerId.value = null;
  selectedCollaborationKey.value = null;
  tab.value = "tasks";
  const cached = taskFromSnapshot(task.id);
  if (!cached) void ensureSelectedTask();
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
  selectedTask.value = null;
  tab.value = "overview";
}

function clearSelection(): void {
  selectedEmployeeId.value = null;
  selectedWorkerId.value = null;
  selectedTaskId.value = null;
  selectedTask.value = null;
  selectedCollaborationKey.value = null;
  tab.value = "overview";
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
    else {
      selectedTaskId.value = selection.id;
      void ensureSelectedTask();
      tab.value = "tasks";
    }
  } else if (selection.kind === "execution") {
    const edge = scene.value?.executionEdges.find((item) => item.key === selection.id);
    if (edge) selectWorker(edge.workerId);
  } else if (selection.kind === "collaboration") {
    selectCollaboration(selection.id);
  }
}

function onInspectorSelect(payload: {
  kind: "employee" | "worker" | "task" | "collaboration";
  id: string;
  task?: NebulaTask;
  employeeId?: string;
}): void {
  if (payload.kind === "employee") selectEmployee(payload.id);
  else if (payload.kind === "worker") selectWorker(payload.id);
  else if (payload.kind === "collaboration") selectCollaboration(payload.id);
  else if (payload.task) selectTask(payload.task);
  else {
    selectedTaskId.value = payload.id;
    if (payload.employeeId) selectedEmployeeId.value = payload.employeeId;
    tab.value = "tasks";
    void ensureSelectedTask();
  }
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
  if (selectedTaskId.value) await ensureSelectedTask();
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
  const employee = typeof query.employee === "string" ? query.employee : stored.employee ?? "";
  const worker = typeof query.worker === "string" ? query.worker : stored.worker ?? "";
  const task = typeof query.task === "string" ? query.task : stored.task ?? "";
  const collaboration = typeof query.collab === "string" ? query.collab : stored.collab ?? "";
  const storedTab = typeof query.tab === "string" ? query.tab : stored.tab ?? "";
  selectedEmployeeId.value = employee || null;
  selectedWorkerId.value = worker || null;
  selectedTaskId.value = task || null;
  selectedCollaborationKey.value = collaboration || null;
  tab.value = ["chat", "tasks", "overview", "attention"].includes(storedTab)
    ? (storedTab as InspectorTab)
    : employee
      ? "chat"
      : "overview";
  if (selectedTaskId.value) void ensureSelectedTask();
}

function syncRoute(): void {
  const query: Record<string, string> = {};
  if (selectedEmployeeId.value) query.employee = selectedEmployeeId.value;
  if (selectedWorkerId.value) query.worker = selectedWorkerId.value;
  if (selectedTaskId.value) query.task = selectedTaskId.value;
  if (selectedCollaborationKey.value) query.collab = selectedCollaborationKey.value;
  if (tab.value !== "overview") query.tab = tab.value;
  const current = route.query;
  const same =
    Object.keys(query).length === Object.keys(current).length &&
    Object.entries(query).every(([key, value]) => current[key] === value);
  if (same) return;
  void router.replace({ path: "/", query });
}

watch(
  [selectedEmployeeId, selectedWorkerId, selectedTaskId, selectedCollaborationKey, tab],
  () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        employee: selectedEmployeeId.value ?? "",
        worker: selectedWorkerId.value ?? "",
        task: selectedTaskId.value ?? "",
        collab: selectedCollaborationKey.value ?? "",
        tab: tab.value,
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
    if (selectedWorkerId.value) {
      const worker = snapshot.workers.find((item) => item.id === selectedWorkerId.value);
      if (!worker) selectedWorkerId.value = null;
      else if (!selectedEmployeeId.value && worker.digitalEmployeeId) {
        selectedEmployeeId.value = worker.digitalEmployeeId;
      }
    }
    if (selectedCollaborationKey.value) {
      const stillThere = snapshot.collaborationEdges.some(
        (edge) => collaborationKey(edge.fromEmployeeId, edge.toEmployeeId) === selectedCollaborationKey.value,
      );
      if (!stillThere) selectedCollaborationKey.value = null;
    }
    if (selectedTaskId.value) {
      const found = taskFromSnapshot(selectedTaskId.value);
      if (found) selectedTask.value = found;
    }
    if (!hydrated) {
      hydrated = true;
      if (selectedTaskId.value) void ensureSelectedTask();
    }
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
  display: flex;
  flex: 1 1 auto;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border-radius: 16px;
  border: 1px solid #0c3b33;
  background: #031a1a;
  box-shadow: 0 20px 50px rgba(3, 30, 25, .18);
}
.nebula-stage {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  background: radial-gradient(ellipse at 49% 43%, #0b3c34 0%, #052923 46%, #031a1a 90%);
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.visual-top { position: absolute; top: 22px; left: 26px; pointer-events: none; z-index: 3; max-width: calc(100% - 520px); }
.eyebrow { font-size: 9px; font-weight: 750; letter-spacing: 2.4px; color: #67cba8; }
.live-indicator {
  display: inline-block; margin-left: 10px; border-radius: 12px; background: #0b543c;
  border: 1px solid #1e9874; padding: 3px 7px; font-size: 8px; letter-spacing: 1px;
  color: #8fffd5; box-shadow: 0 0 14px #1fa67c30;
}
.live-indicator[data-state="stale"] { border-color: #a27a37; color: #ffd39a; background: #4a3512; }
.live-indicator[data-state="offline"] { border-color: #a4545e; color: #ffb0bb; background: #4b1b22; }
.live-indicator[data-state="connecting"] { color: #9dbab0; border-color: #3d6459; background: #123b33; }
.visual-top h1 { margin: 7px 0 6px; font-size: 23px; font-weight: 740; letter-spacing: .2px; color: #e6fff2; text-shadow: 0 0 14px #0e574760; }
.visual-top p { margin: 0; font-size: 11px; color: #85afa1; }
.top-right { position: absolute; right: 22px; top: 22px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; z-index: 4; max-width: calc(100% - 40px); }
.top-right button {
  background: #103f38df; color: #b7e2d3; border: 1px solid #366a5d; border-radius: 9px;
  padding: 7px 10px; font-size: 10px; cursor: pointer; white-space: nowrap;
}
.top-right button:hover { background: #155246; }
.top-right button.pressed { background: #128e6f; color: #e2fff4; border-color: #5de6bb; box-shadow: 0 0 18px #2fc39455; }
.search {
  position: absolute; right: 22px; top: 66px; z-index: 4; display: flex; align-items: center; gap: 6px;
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
.guide-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 10px; color: #c6f9df; font-weight: 750; }
.guide-title small { font-size: 9px; color: #8bc0aa; font-weight: 450; line-height: 1.5; }
.legend { display: flex; align-items: center; flex-wrap: wrap; gap: 13px; margin-top: 9px; color: #91baa9; font-size: 9px; }
.legend span::before {
  content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--c); margin-right: 5px; vertical-align: 1px;
}
.tip { position: absolute; right: 22px; bottom: 18px; color: #88bba8; font-size: 9px; text-align: right; pointer-events: none; z-index: 3; max-width: 330px; line-height: 1.6; }
.stage-state {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
  z-index: 5; text-align: center; color: #b7e2d3; font-size: 12px; line-height: 1.8;
  background: #052920d9; border: 1px solid #ffffff21; border-radius: 14px;
  padding: 22px 26px; max-width: 420px; backdrop-filter: blur(12px);
}
.stage-state.error { border-color: #a4545e; color: #ffb9c3; }
.stage-state button { margin-top: 8px; border: 1px solid #347b67; background: #0d483c; color: #d5f4e9; border-radius: 7px; padding: 6px 10px; font-size: 11px; cursor: pointer; }
.state-orbit { font-size: 30px; color: #4fe0b3; margin-bottom: 6px; }
.state-link { display: inline-flex; margin-top: 10px; color: #8fffd5; font-size: 11px; }
.stage-notice {
  position: absolute; left: 50%; top: 16px; transform: translateX(-50%);
  z-index: 6; background: #4a3512e8; border: 1px solid #a27a37; color: #ffe0b8;
  border-radius: 9px; padding: 7px 11px; font-size: 10px; display: flex; gap: 8px; align-items: center;
  max-width: min(620px, calc(100% - 40px)); line-height: 1.5;
}
.stage-notice button { border: 0; background: #79531d; color: #fff0d2; border-radius: 5px; font-size: 9px; padding: 3px 7px; cursor: pointer; }
@media (max-width: 1500px) {
  .visual-top { max-width: calc(100% - 520px); }
  .visual-top h1 { font-size: 20px; }
  .guide { max-width: calc(100% - 390px); }
}
@media (max-width: 1180px) {
  .visual-top { display: none; }
  .top-right { max-width: 430px; }
  .guide { max-width: calc(100% - 360px); }
  .tip { display: none; }
}
@media (max-width: 900px) {
  .nebula-home { flex-direction: column; height: 100%; overflow: auto; }
  .nebula-stage { flex: 1 1 54vh; min-height: 46vh; }
  .top-right { right: 12px; top: 12px; max-width: calc(100% - 24px); }
  .visual-top { left: 14px; top: 14px; max-width: calc(100% - 180px); }
  .visual-top h1 { font-size: 16px; }
  .search { right: 12px; top: 58px; }
  .guide { left: 12px; bottom: 12px; max-width: calc(100% - 24px); }
  .guide-title small { display: none; }
  .legend { gap: 9px; }
}
</style>
