<template>
  <section
    class="pixel-office"
    :class="{ 'motion-paused': reduceMotion || hidden }"
    aria-label="Nexus 数字办公室"
  >
    <OfficeScene
      :employees="visibleEmployees"
      :snapshot="store.snapshot"
      :selected-id="selectedId"
      :reduced-motion="
        reduceMotion || hidden || !!store.error || store.connection !== 'live'
      "
      :flashes="flashes"
      @select="selectEmployee"
    />

    <Transition name="panel">
      <EmployeeWorkbench
        v-if="selectedEmployee"
        :key="selectedEmployee.id"
        :employee="selectedEmployee"
        :data="office"
        :loading="officeLoading"
        :error="officeError"
        @close="selectEmployee(null)"
        @refresh="loadOffice"
        @worker-page="setWorkerPage"
        @issue-page="setIssuePage"
      />
    </Transition>

    <div v-if="!store.snapshot" class="office-message" role="status">
      <span class="office-message__icon" :class="{ 'is-error': !!store.error }" aria-hidden="true">
        <AlertCircle v-if="store.error" :size="20" :stroke-width="1.7" />
        <Loader2 v-else class="office-spin" :size="20" :stroke-width="1.8" />
      </span>
      <h1>{{ store.error ? "暂时无法读取办公室" : "办公室准备中" }}</h1>
      <p>{{ store.error || "正在同步数字员工与任务状态…" }}</p>
      <button v-if="store.error" type="button" @click="refresh">重新加载</button>
    </div>

    <div v-else-if="!store.employees.length" class="office-message">
      <span class="office-message__icon" aria-hidden="true">
        <UsersRound :size="20" :stroke-width="1.7" />
      </span>
      <h1>配置第一位数字员工</h1>
      <p>完成配置后，员工将自动出现在对应工位</p>
      <RouterLink to="/settings/employees">
        配置数字员工 <ArrowUpRight :size="14" />
      </RouterLink>
    </div>

    <div
      v-if="
        store.snapshot && (store.error || store.snapshot.degradedSources.length)
      "
      class="office-notice"
      role="alert"
    >
      <AlertCircle :size="14" />
      <span>{{ store.error || "部分状态待确认 · 显示最近快照" }}</span>
      <button type="button" :disabled="store.loading" @click="refresh">重试</button>
    </div>

    <div
      v-if="menuOpen"
      class="office-menu-backdrop"
      aria-hidden="true"
      @click="menuOpen = false"
    />

    <Transition name="menu">
      <nav v-if="menuOpen" class="office-menu" aria-label="Nexus 模块入口">
        <div class="menu-title">
          <strong>NEXUS</strong>
          <span>工作空间</span>
        </div>
        <div class="menu-group-label">协作与任务</div>
        <RouterLink
          v-for="item in workspaceNavItems"
          :key="item.to"
          :to="item.to"
          @click="menuOpen = false"
        >
          <component :is="navIcon(item.to)" :size="15" />
          <span>{{ item.label }}</span>
          <ArrowUpRight :size="13" class="menu-arrow" />
        </RouterLink>
        <div class="menu-divider" />
        <div class="menu-group-label">平台与运维</div>
        <RouterLink
          v-for="item in opsNavItems"
          :key="item.to"
          :to="item.to"
          @click="menuOpen = false"
        >
          <component :is="navIcon(item.to)" :size="15" />
          <span>{{ item.label }}</span>
          <ArrowUpRight :size="13" class="menu-arrow" />
        </RouterLink>
        <RouterLink to="/settings/workers" @click="menuOpen = false">
          <Monitor :size="15" />
          <span>Agent Worker</span>
          <ArrowUpRight :size="13" class="menu-arrow" />
        </RouterLink>
      </nav>
    </Transition>

    <div
      v-if="
        !selectedId &&
        visibleEmployees.length > 0 &&
        visibleEmployees.length <= 3
      "
      class="office-guide"
    >
      <span>点击工位查看员工详情</span>
    </div>

    <div class="office-dock">
      <button
        class="menu-toggle"
        :class="{ 'is-open': menuOpen }"
        type="button"
        :aria-expanded="menuOpen"
        :aria-label="menuOpen ? '关闭 Nexus 模块入口' : '打开 Nexus 模块入口'"
        @click="menuOpen = !menuOpen"
      >
        <component :is="menuOpen ? X : PanelsTopLeft" :size="16" :stroke-width="1.8" />
        <span>工作空间</span>
      </button>

      <div class="dock-divider" aria-hidden="true" />

      <label class="employee-select">
        <UsersRound :size="14" :stroke-width="1.7" />
        <select
          aria-label="选择数字员工"
          :value="selectedId ?? ''"
          @change="onSelectEmployee"
        >
          <option value="">办公室全景 ({{ store.employees.length }})</option>
          <option
            v-for="employee in store.employees"
            :key="employee.id"
            :value="employee.id"
          >
            {{ employee.name }}
          </option>
        </select>
        <ChevronDown class="employee-select__caret" :size="13" aria-hidden="true" />
      </label>

      <div v-if="store.employees.length > OFFICE_ROOM_SIZE" class="room-pager">
        <button
          type="button"
          aria-label="上一间办公室"
          :disabled="room === 1"
          @click="changeRoom(-1)"
        >
          <ChevronLeft :size="14" />
        </button>
        <span class="room-pager__num">{{ room }} / {{ roomCount }}</span>
        <button
          type="button"
          aria-label="下一间办公室"
          :disabled="room === roomCount"
          @click="changeRoom(1)"
        >
          <ChevronRight :size="14" />
        </button>
      </div>

      <div class="dock-divider dock-divider--soft" aria-hidden="true" />

      <span
        class="connection"
        :data-state="store.error ? 'stale' : store.connection"
        :title="connectionLabel"
      >
        <i aria-hidden="true" />
        <span>{{ connectionLabel }}</span>
      </span>

      <button
        class="dock-icon"
        type="button"
        :disabled="store.loading || officeLoading"
        aria-label="刷新办公室"
        title="刷新办公室"
        @click="refresh"
      >
        <RefreshCw :size="14" :class="{ 'office-spin': store.loading || officeLoading }" />
      </button>

      <button
        class="dock-icon motion-toggle"
        type="button"
        :aria-pressed="reduceMotion"
        :aria-label="reduceMotion ? '启用办公室动效' : '减少办公室动效'"
        :title="reduceMotion ? '启用动效' : '暂停动效'"
        @click="toggleMotion"
      >
        <component :is="reduceMotion ? Play : Pause" :size="14" />
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import {
  AlertCircle,
  ArrowUpRight,
  BookOpen,
  CalendarClock,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ListTodo,
  Loader2,
  MessageSquare,
  Monitor,
  Network,
  PanelsTopLeft,
  Pause,
  Play,
  RefreshCw,
  Settings,
  UsersRound,
  X,
} from "lucide-vue-next";
import OfficeScene from "../features/office/OfficeScene.vue";
import EmployeeWorkbench from "../features/office/EmployeeWorkbench.vue";
import { getEmployeeOffice } from "../features/office/api";
import {
  OFFICE_ROOM_SIZE,
  type EmployeeOffice,
} from "../features/office/model";
import { useNebulaStore } from "../features/nebula/stores/nebulaStore";
import { getVisibleNavItems } from "../navigation";

const store = useNebulaStore();
const route = useRoute();
const router = useRouter();

const selectedId = ref<string | null>(null);
const room = ref(1);
const menuOpen = ref(false);
const office = ref<EmployeeOffice | null>(null);
const officeLoading = ref(false);
const officeError = ref("");
const workerPage = ref(1);
const issuePage = ref(1);
const reduceMotion = ref(false);
const hidden = ref(document.hidden);
const flashes = ref<Record<string, boolean>>({});

const roomCount = computed(() =>
  Math.max(1, Math.ceil(store.employees.length / OFFICE_ROOM_SIZE)),
);

const visibleEmployees = computed(() =>
  store.employees.slice(
    (room.value - 1) * OFFICE_ROOM_SIZE,
    room.value * OFFICE_ROOM_SIZE,
  ),
);

const selectedEmployee = computed(
  () => store.employees.find((e) => e.id === selectedId.value) ?? null,
);

const navItems = getVisibleNavItems("/");
const workspaceNavItems = navItems.filter((item) => !["/gateway", "/settings"].includes(item.to));
const opsNavItems = navItems.filter((item) => ["/gateway", "/settings"].includes(item.to));

const navIconMap: Record<string, unknown> = {
  "/chat": MessageSquare,
  "/schedules": CalendarClock,
  "/tasks": ListTodo,
  "/library": BookOpen,
  "/gateway": Network,
  "/settings": Settings,
};

function navIcon(to: string) {
  return navIconMap[to] ?? PanelsTopLeft;
}

const connectionLabel = computed(() =>
  store.error
    ? "更新失败"
    : store.connection === "live"
      ? "实时同步"
      : store.connection === "stale"
        ? "重连中"
        : store.connection === "offline"
          ? "连接中断"
          : "连接中",
);

let controller: AbortController | null = null;
let requestSequence = 0;
let flashTimer = 0;
let motionMedia: MediaQueryList | null = null;
let motionTouched = false;

function selectEmployee(id: string | null) {
  menuOpen.value = false;
  if (id) {
    const index = store.employees.findIndex((e) => e.id === id);
    if (index < 0) return;
    room.value = Math.floor(index / OFFICE_ROOM_SIZE) + 1;
  }
  selectedId.value = id;
  void router.replace({ path: "/", query: id ? { employee: id } : {} });
}

function onSelectEmployee(event: Event) {
  selectEmployee((event.target as HTMLSelectElement).value || null);
}

function changeRoom(delta: number) {
  selectEmployee(null);
  room.value = Math.max(1, Math.min(roomCount.value, room.value + delta));
}

async function loadOffice() {
  const id = selectedId.value;
  controller?.abort();
  const sequence = ++requestSequence;
  if (!id) {
    officeLoading.value = false;
    return;
  }
  controller = new AbortController();
  officeLoading.value = true;
  try {
    const result = await getEmployeeOffice(
      id,
      workerPage.value,
      issuePage.value,
      controller.signal,
    );
    if (sequence !== requestSequence || id !== selectedId.value) return;
    office.value = result;
    workerPage.value = result.workerPage;
    issuePage.value = result.issuePage;
    officeError.value = "";
  } catch (error) {
    if (
      sequence !== requestSequence ||
      (error instanceof DOMException && error.name === "AbortError")
    )
      return;
    officeError.value =
      error instanceof Error ? error.message : "工作台更新失败";
  } finally {
    if (sequence === requestSequence) officeLoading.value = false;
  }
}

function setWorkerPage(page: number) {
  workerPage.value = page;
  void loadOffice();
}

function setIssuePage(page: number) {
  issuePage.value = page;
  void loadOffice();
}

async function refresh() {
  const before = store.lastConfirmedAt;
  await store.refresh();
  if (selectedId.value && store.lastConfirmedAt === before) await loadOffice();
}

function toggleMotion() {
  reduceMotion.value = !reduceMotion.value;
  motionTouched = true;
  localStorage.setItem(
    "nexus_office_reduce_motion",
    reduceMotion.value ? "1" : "0",
  );
}

function onMotionChange() {
  if (!motionTouched) reduceMotion.value = motionMedia?.matches ?? false;
}

function onVisibility() {
  hidden.value = document.hidden;
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    if (menuOpen.value) menuOpen.value = false;
    else selectEmployee(null);
  }
}

watch(selectedId, () => {
  office.value = null;
  officeError.value = "";
  workerPage.value = 1;
  issuePage.value = 1;
  void loadOffice();
});

watch(
  () => route.query.employee,
  (id) => {
    const value = typeof id === "string" ? id : null;
    if (value !== selectedId.value) {
      selectedId.value =
        value && store.employees.some((e) => e.id === value) ? value : null;
      if (selectedId.value)
        room.value =
          Math.floor(
            store.employees.findIndex((e) => e.id === selectedId.value) /
              OFFICE_ROOM_SIZE,
          ) + 1;
    }
  },
);

watch(
  () => store.snapshot,
  (next, previous) => {
    if (!next) return;
    if (
      selectedId.value &&
      !next.employees.some((e) => e.id === selectedId.value)
    )
      selectEmployee(null);
    const queryId =
      typeof route.query.employee === "string" ? route.query.employee : null;
    if (!previous && queryId && next.employees.some((e) => e.id === queryId))
      selectEmployee(queryId);
    room.value = Math.min(room.value, roomCount.value);
    if (previous && !next.degradedSources.includes("tasks")) {
      const changed: Record<string, boolean> = {};
      for (const employee of next.employees) {
        const before = previous.employees.find((e) => e.id === employee.id);
        if (
          before &&
          employee.counts.completedToday > before.counts.completedToday
        )
          changed[employee.id] = true;
      }
      flashes.value = changed;
      window.clearTimeout(flashTimer);
      flashTimer = window.setTimeout(() => {
        flashes.value = {};
      }, 4500);
    }
    if (selectedId.value) void loadOffice();
  },
);

onMounted(() => {
  motionMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
  const saved = localStorage.getItem("nexus_office_reduce_motion");
  motionTouched = saved === "1" || saved === "0";
  reduceMotion.value = motionTouched ? saved === "1" : motionMedia.matches;
  motionMedia.addEventListener("change", onMotionChange);
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("keydown", onKeydown);
  const initial =
    typeof route.query.employee === "string" ? route.query.employee : null;
  if (initial && store.employees.some((e) => e.id === initial))
    selectEmployee(initial);
  store.start();
});

onBeforeUnmount(() => {
  store.stop();
  controller?.abort();
  requestSequence++;
  window.clearTimeout(flashTimer);
  document.removeEventListener("visibilitychange", onVisibility);
  document.removeEventListener("keydown", onKeydown);
  motionMedia?.removeEventListener("change", onMotionChange);
});
</script>

<style scoped>
.pixel-office {
  height: 100%;
  width: 100%;
  position: relative;
  overflow: hidden;
  background: #eceef2;
  color: var(--astra-text-primary);
  font-family: var(--astra-font-family);
}

/* ── 底部悬浮 Dock ────────────────────────────────────────────── */
.office-dock {
  position: absolute;
  z-index: 12;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 6px;
  height: 44px;
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(16px);
  border: 1px solid var(--astra-border);
  box-shadow: var(--astra-shadow-float);
  border-radius: 12px;
  padding: 0 8px;
  max-width: calc(100% - 24px);
  box-sizing: border-box;
  white-space: nowrap;
}

.office-dock button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 30px;
  color: var(--astra-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
  padding: 0 9px;
  font-size: 12.5px;
  font-weight: 500;
  transition: background-color 0.14s ease, color 0.14s ease, border-color 0.14s ease;
}

.office-dock button:hover {
  background: var(--astra-bg-elevated);
  color: var(--astra-text-primary);
}

.office-dock button:disabled {
  opacity: 0.35;
  cursor: default;
}

.menu-toggle {
  color: var(--astra-text-primary) !important;
  font-weight: 600 !important;
}

.menu-toggle.is-open {
  background: var(--astra-primary) !important;
  color: #ffffff !important;
}

.dock-icon {
  width: 30px;
  padding: 0 !important;
}

.dock-divider {
  height: 18px;
  width: 1px;
  background: var(--astra-border);
  flex: none;
}

.dock-divider--soft {
  background: var(--astra-border-subtle);
}

.employee-select {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 8px;
  border-radius: 8px;
  color: var(--astra-text-muted);
  transition: background-color 0.14s ease;
}

.employee-select:hover {
  background: var(--astra-bg-base);
}

.employee-select select {
  appearance: none;
  background: transparent;
  border: 0;
  color: var(--astra-text-primary);
  font-size: 12.5px;
  font-weight: 500;
  max-width: 156px;
  padding: 0 18px 0 2px;
  height: 100%;
  cursor: pointer;
}

.employee-select select:focus {
  outline: none;
}

.employee-select__caret {
  position: absolute;
  right: 7px;
  pointer-events: none;
  color: var(--astra-text-muted);
}

.room-pager {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: var(--astra-text-muted);
}

.room-pager button {
  width: 24px;
  height: 24px;
  padding: 0;
}

.room-pager__num {
  padding: 0 3px;
  font-family: var(--astra-font-mono);
  font-size: 11px;
  color: var(--astra-text-secondary);
}

.connection {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--astra-bg-subtle);
  border: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-secondary);
  font-size: 11.5px;
  font-weight: 500;
}

.connection i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--astra-success);
  flex: none;
}

.connection[data-state="stale"] i,
.connection[data-state="offline"] i {
  background: var(--astra-warning);
}

.connection[data-state="connecting"] i {
  background: var(--astra-neutral);
}

.office-spin {
  animation: office-rotate 0.9s linear infinite;
}

@keyframes office-rotate {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ── 轻引导胶囊 ───────────────────────────────────────────────── */
.office-guide {
  position: absolute;
  bottom: 70px;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
  pointer-events: none;
  white-space: nowrap;
  z-index: 6;
}

.office-guide span {
  display: inline-flex;
  align-items: center;
  padding: 4px 12px;
  border-radius: 999px;
  border: 1px solid var(--astra-border);
  background: rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(10px);
  color: var(--astra-text-secondary);
  font-size: 12px;
  box-shadow: var(--astra-shadow-panel);
}

/* ── 模块入口浮层菜单 ─────────────────────────────────────────── */
.office-menu-backdrop {
  position: absolute;
  inset: 0;
  z-index: 10;
}

.office-menu {
  position: absolute;
  z-index: 11;
  left: 50%;
  bottom: 66px;
  transform: translateX(-50%);
  width: 248px;
  background: rgba(255, 255, 255, 0.97);
  backdrop-filter: blur(16px);
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  padding: 8px;
  box-shadow: var(--astra-shadow-float);
}

.menu-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px 9px;
  border-bottom: 1px solid var(--astra-border-subtle);
  margin-bottom: 6px;
}

.menu-title strong {
  font-size: 12.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--astra-text-primary);
}

.menu-title span {
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
  font-size: 10.5px;
  font-weight: 500;
}

.menu-group-label {
  padding: 5px 8px 3px;
  font-size: 11px;
  font-weight: 600;
  color: var(--astra-text-muted);
}

.menu-divider {
  height: 1px;
  margin: 6px 4px;
  background: var(--astra-border-subtle);
}

.office-menu a {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 34px;
  padding: 0 9px;
  text-decoration: none;
  color: var(--astra-text-secondary);
  font-size: 13px;
  font-weight: 500;
  border-radius: 8px;
  transition: background-color 0.14s ease, color 0.14s ease;
}

.office-menu a > span {
  flex: 1;
}

.office-menu .menu-arrow {
  color: var(--astra-text-muted);
  opacity: 0;
  transition: opacity 0.14s ease, transform 0.14s ease;
}

.office-menu a:hover {
  background: var(--astra-bg-elevated);
  color: var(--astra-text-primary);
}

.office-menu a:hover .menu-arrow {
  opacity: 1;
  transform: translate(1px, -1px);
}

/* ── 中央状态/空态卡 ──────────────────────────────────────────── */
.office-message {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: min(380px, calc(100% - 48px));
  padding: 24px 22px;
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(14px);
  border: 1px solid var(--astra-border);
  border-radius: 14px;
  box-shadow: var(--astra-shadow-float);
  text-align: center;
  z-index: 5;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.office-message__icon {
  display: inline-grid;
  place-items: center;
  width: 40px;
  height: 40px;
  margin-bottom: 12px;
  border-radius: 10px;
  border: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-primary);
}

.office-message__icon.is-error {
  border-color: color-mix(in srgb, var(--astra-danger) 28%, transparent);
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
}

.office-message h1 {
  font-size: 17px;
  font-weight: 650;
  margin: 0 0 6px;
  color: var(--astra-text-primary);
}

.office-message p {
  margin: 0;
  font-size: 13px;
  color: var(--astra-text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.office-message a,
.office-message button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  background: var(--astra-primary);
  border: 0;
  border-radius: 8px;
  color: #ffffff;
  padding: 0 16px;
  margin-top: 16px;
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.14s ease;
}

.office-message a:hover,
.office-message button:hover {
  background: var(--astra-primary-bright);
}

/* ── 降级警示条 ───────────────────────────────────────────────── */
.office-notice {
  position: absolute;
  left: 16px;
  bottom: 70px;
  max-width: calc(100% - 32px);
  z-index: 9;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(255, 251, 235, 0.96);
  backdrop-filter: blur(10px);
  color: var(--astra-warning);
  padding: 7px 11px;
  border: 1px solid color-mix(in srgb, var(--astra-warning) 30%, transparent);
  border-radius: 9px;
  box-shadow: var(--astra-shadow-panel);
  font-size: 12px;
  line-height: 1.45;
}

.office-notice svg {
  flex: none;
}

.office-notice button {
  border: 0;
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
}

/* ── 过渡动效 ─────────────────────────────────────────────────── */
.panel-enter-active,
.panel-leave-active {
  transition: transform 0.22s ease, opacity 0.22s ease;
}

.panel-enter-from,
.panel-leave-to {
  transform: translateX(28px);
  opacity: 0;
}

.menu-enter-active,
.menu-leave-active {
  transition: transform 0.16s ease, opacity 0.16s ease;
}

.menu-enter-from,
.menu-leave-to {
  transform: translate(-50%, 6px);
  opacity: 0;
}

.motion-paused :deep(.robot) {
  animation-play-state: paused !important;
}

button:focus-visible,
a:focus-visible,
select:focus-visible {
  outline: 2px solid var(--astra-primary);
  outline-offset: 2px;
}

@media (min-width: 2240px) {
  .office-dock {
    height: 48px;
    bottom: 18px;
    gap: 8px;
    padding: 0 12px;
    border-radius: 14px;
  }
  .office-dock button {
    height: 34px;
    padding: 0 11px;
    font-size: 13px;
  }
  .dock-icon {
    width: 34px;
  }
  .employee-select {
    height: 34px;
    padding: 0 10px;
  }
  .employee-select select {
    max-width: 210px;
    font-size: 13px;
  }
  .connection {
    height: 26px;
    padding: 0 10px;
    font-size: 12px;
  }
  .office-guide {
    bottom: 78px;
  }
  .office-menu {
    width: 280px;
    bottom: 76px;
    padding: 10px;
    border-radius: 14px;
  }
  .office-menu a {
    height: 36px;
    font-size: 13.5px;
  }
}

@media (max-width: 759px) {
  .office-dock {
    gap: 3px;
    height: 40px;
    bottom: 10px;
    padding: 0 6px;
    max-width: calc(100% - 20px);
  }
  .menu-toggle span {
    display: none;
  }
  .employee-select select {
    max-width: 118px;
    font-size: 11.5px;
  }
  .connection span {
    display: none;
  }
  .connection {
    padding: 0 6px;
  }
  .motion-toggle {
    display: none !important;
  }
  .office-guide {
    bottom: 60px;
  }
  .office-guide span {
    font-size: 11px;
  }
  .office-notice {
    bottom: 60px;
    left: 10px;
    max-width: calc(100% - 20px);
    font-size: 11px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .panel-enter-active,
  .panel-leave-active,
  .menu-enter-active,
  .menu-leave-active {
    transition: none;
  }
  .office-spin {
    animation: none;
  }
}
</style>
