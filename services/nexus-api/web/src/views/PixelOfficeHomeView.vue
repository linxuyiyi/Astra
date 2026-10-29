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
    <Transition name="panel"
      ><EmployeeWorkbench
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
    /></Transition>
    <div v-if="!store.snapshot" class="office-message" role="status">
      <span class="message-eyebrow">NEXUS / 数字办公室</span>
      <h1>{{ store.error ? "暂时无法读取办公室" : "办公室准备中" }}</h1>
      <p>{{ store.error || "正在读取数字员工与工作计划…" }}</p>
      <button v-if="store.error" type="button" @click="refresh">
        重新加载
      </button>
    </div>
    <div v-else-if="!store.employees.length" class="office-message">
      <span class="message-eyebrow">WELCOME TO NEXUS</span>
      <h1>为团队迎来第一位数字员工</h1>
      <p>配置数字员工后，他们会在这里拥有自己的工位。</p>
      <RouterLink to="/settings/employees"
        >配置数字员工 <ArrowUpRight :size="14"
      /></RouterLink>
    </div>
    <div
      v-if="
        store.snapshot && (store.error || store.snapshot.degradedSources.length)
      "
      class="office-notice"
      role="alert"
    >
      <AlertCircle :size="15" /><span
        >{{ store.error || "部分数据来源暂不可用，工作状态待确认" }} ·
        当前显示最近确认的快照</span
      ><button type="button" :disabled="store.loading" @click="refresh">
        重试
      </button>
    </div>
    <nav v-if="menuOpen" class="office-menu" aria-label="Nexus 模块入口">
      <div class="menu-title">NEXUS <span>工作空间</span></div>
      <RouterLink v-for="(item, index) in navItems" :key="item.to" :to="item.to"
        ><component :is="navIcons[index]" :size="17" /><span>{{
          item.label
        }}</span
        ><ArrowUpRight :size="13" /></RouterLink
      ><RouterLink to="/settings/workers"
        ><Monitor :size="17" /><span>共享 Worker</span><ArrowUpRight :size="13"
      /></RouterLink>
    </nav>
    <div class="office-dock">
      <button
        class="menu-toggle"
        type="button"
        :aria-expanded="menuOpen"
        aria-label="打开 Nexus 模块入口"
        @click="menuOpen = !menuOpen"
      >
        <component :is="menuOpen ? X : PanelsTopLeft" :size="18" /><span
          >工作空间</span
        >
      </button>
      <div class="dock-divider" />
      <label class="employee-select"
        ><UsersRound :size="15" /><select
          aria-label="定位数字员工"
          :value="selectedId ?? ''"
          @change="onSelectEmployee"
        >
          <option value="">办公室全景</option>
          <option
            v-for="employee in store.employees"
            :key="employee.id"
            :value="employee.id"
          >
            {{ employee.name }}
          </option>
        </select></label
      >
      <div v-if="store.employees.length > 6" class="room-pager">
        <button
          type="button"
          aria-label="上一间办公室"
          :disabled="room === 1"
          @click="changeRoom(-1)"
        >
          <ChevronLeft :size="14" /></button
        ><span>{{ room }} / {{ roomCount }}</span
        ><button
          type="button"
          aria-label="下一间办公室"
          :disabled="room === roomCount"
          @click="changeRoom(1)"
        >
          <ChevronRight :size="14" />
        </button>
      </div>
      <span
        class="connection"
        :data-state="store.error ? 'stale' : store.connection"
        ><i />{{ connectionLabel }}</span
      >
      <button
        class="dock-icon"
        type="button"
        :disabled="store.loading || officeLoading"
        aria-label="刷新办公室"
        @click="refresh"
      >
        <RefreshCw :size="15" />
      </button>
      <button
        class="dock-icon motion-toggle"
        type="button"
        :aria-pressed="reduceMotion"
        :aria-label="reduceMotion ? '启用办公室动效' : '减少办公室动效'"
        @click="toggleMotion"
      >
        <component :is="reduceMotion ? Play : Pause" :size="15" />
      </button>
    </div>
    <div
      v-if="
        !selectedId &&
        visibleEmployees.length > 0 &&
        visibleEmployees.length <= 3
      "
      class="office-guide"
    >
      <span>点击工位，走近一位同事</span><small>点击空白返回全景</small>
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
  ChevronLeft,
  ChevronRight,
  ListTodo,
  MessageSquare,
  Monitor,
  PanelsTopLeft,
  Pause,
  Play,
  RefreshCw,
  Settings,
  UsersRound,
  Waypoints,
  X,
} from "lucide-vue-next";
import OfficeScene from "../features/office/OfficeScene.vue";
import EmployeeWorkbench from "../features/office/EmployeeWorkbench.vue";
import { getEmployeeOffice } from "../features/office/api";
import type { EmployeeOffice } from "../features/office/model";
import { useNebulaStore } from "../features/nebula/stores/nebulaStore";
import { getVisibleNavItems } from "../navigation";
const store = useNebulaStore();
const route = useRoute(),
  router = useRouter();
const selectedId = ref<string | null>(null),
  room = ref(1),
  menuOpen = ref(false);
const office = ref<EmployeeOffice | null>(null),
  officeLoading = ref(false),
  officeError = ref("");
const workerPage = ref(1),
  issuePage = ref(1);
const reduceMotion = ref(false),
  hidden = ref(document.hidden);
const flashes = ref<Record<string, boolean>>({});
const roomCount = computed(() =>
  Math.max(1, Math.ceil(store.employees.length / 6)),
);
const visibleEmployees = computed(() =>
  store.employees.slice((room.value - 1) * 6, room.value * 6),
);
const selectedEmployee = computed(
  () => store.employees.find((e) => e.id === selectedId.value) ?? null,
);
const navItems = getVisibleNavItems("/");
const navIcons = [MessageSquare, ListTodo, BookOpen, Waypoints, Settings];
const connectionLabel = computed(() =>
  store.error
    ? "更新失败"
    : store.connection === "live"
      ? "自动更新"
      : store.connection === "stale"
        ? "正在重连"
        : store.connection === "offline"
          ? "连接中断"
          : "连接中",
);
let controller: AbortController | null = null,
  requestSequence = 0,
  flashTimer = 0;
let motionMedia: MediaQueryList | null = null,
  motionTouched = false;
function selectEmployee(id: string | null) {
  menuOpen.value = false;
  if (id) {
    const index = store.employees.findIndex((e) => e.id === id);
    if (index < 0) return;
    room.value = Math.floor(index / 6) + 1;
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
            store.employees.findIndex((e) => e.id === selectedId.value) / 6,
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
  background: #e9ddc5;
  color: #3d654d;
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.office-dock {
  position: absolute;
  z-index: 12;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 8px;
  background: #fcfcf0f5;
  border: 1px solid #bacdb1;
  box-shadow:
    0 5px 20px #4766441a,
    0 2px 0 #9eb29555;
  border-radius: 10px;
  padding: 7px 10px;
  max-width: calc(100% - 26px);
  box-sizing: border-box;
  white-space: nowrap;
}
.office-dock button {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #53735a;
  background: transparent;
  border: 0;
  border-radius: 5px;
  cursor: pointer;
  padding: 6px;
  font-size: 11px;
}
.office-dock button:hover {
  background: #e8eede;
}
.office-dock button:disabled {
  opacity: 0.35;
  cursor: default;
}
.menu-toggle span {
  font-size: 11px;
  font-weight: 700;
}
.dock-divider {
  height: 19px;
  width: 1px;
  background: #dce4cf;
}
.employee-select {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #7d9377;
}
.employee-select select {
  background: transparent;
  border: 0;
  color: #587456;
  font-size: 11px;
  max-width: 150px;
  padding: 6px 0;
  cursor: pointer;
}
.connection {
  color: #83957b;
  font-size: 9px;
  padding: 0 5px;
}
.connection i {
  display: inline-block;
  width: 5px;
  height: 5px;
  background: #7ba379;
  margin-right: 5px;
}
.connection[data-state="stale"] i,
.connection[data-state="offline"] i {
  background: #c88d53;
}
.room-pager {
  display: flex;
  align-items: center;
  font-size: 10px;
  color: #829476;
}
.room-pager button {
  padding: 3px;
}
.office-guide {
  position: absolute;
  bottom: 84px;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
  pointer-events: none;
  color: #7d8c70;
  white-space: nowrap;
}
.office-guide span {
  font-size: 12px;
  letter-spacing: 2px;
}
.office-guide small {
  font-size: 9px;
  display: block;
  margin-top: 7px;
  color: #8e9b80;
  letter-spacing: 1px;
}
.office-menu {
  position: absolute;
  z-index: 11;
  left: 50%;
  bottom: 74px;
  transform: translateX(-50%);
  width: 240px;
  background: #fbfcf1;
  border: 1px solid #b9cdb2;
  border-radius: 9px;
  padding: 12px;
  box-shadow: 0 8px 28px #385e3d2b;
}
.menu-title {
  font-size: 12px;
  font-weight: 900;
  color: #406a4d;
  letter-spacing: 1px;
  padding: 7px 9px 12px;
  border-bottom: 1px solid #e1e7d7;
  margin-bottom: 5px;
}
.menu-title span {
  font-size: 9px;
  font-weight: 500;
  margin-left: 7px;
  color: #94a188;
}
.office-menu a {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 10px;
  text-decoration: none;
  color: #617b5e;
  font-size: 12px;
  border-radius: 5px;
}
.office-menu a > span {
  flex: 1;
}
.office-menu a:hover {
  background: #e9f0de;
}
.office-message {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: min(400px, calc(100% - 65px));
  padding: 30px;
  background: #fbfcf1ed;
  border: 1px solid #bfd1b4;
  border-radius: 10px;
  box-shadow: 0 8px 28px #5b775f1a;
  text-align: center;
  z-index: 5;
}
.message-eyebrow {
  font-size: 9px;
  letter-spacing: 2px;
  color: #8a9c80;
}
.office-message h1 {
  font-size: 21px;
  font-weight: 750;
  margin: 16px 0 10px;
}
.office-message p {
  font-size: 12px;
  color: #8a957f;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.office-message a,
.office-message button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: #588363;
  border: 0;
  border-radius: 6px;
  color: #fffff4;
  padding: 10px 16px;
  margin-top: 14px;
  text-decoration: none;
  font-size: 12px;
  cursor: pointer;
}
.office-notice {
  position: absolute;
  left: 20px;
  bottom: 75px;
  max-width: calc(100% - 80px);
  z-index: 9;
  display: flex;
  align-items: center;
  gap: 8px;
  background: #fff2dcf5;
  color: #a37346;
  padding: 10px 13px;
  border: 1px solid #dac7a5;
  border-radius: 7px;
  font-size: 10px;
  line-height: 1.7;
}
.office-notice svg {
  flex-shrink: 0;
}
.office-notice button {
  border: 0;
  background: #ead4b3;
  color: #92663e;
  border-radius: 4px;
  padding: 4px 7px;
  cursor: pointer;
}
.panel-enter-active,
.panel-leave-active {
  transition:
    transform 0.3s,
    opacity 0.3s;
}
.panel-enter-from,
.panel-leave-to {
  transform: translateX(35px);
  opacity: 0;
}
.motion-paused :deep(.robot) {
  animation-play-state: paused !important;
}
button:focus-visible,
a:focus-visible,
select:focus-visible {
  outline: 2px solid #568365;
  outline-offset: 3px;
}
@media (max-width: 759px) {
  .office-dock {
    gap: 3px;
    bottom: 10px;
    padding: 6px 7px;
    max-width: calc(100% - 20px);
  }
  .menu-toggle span {
    display: none;
  }
  .employee-select select {
    max-width: 124px;
    font-size: 10px;
  }
  .connection {
    font-size: 8px;
    padding: 0 3px;
  }
  .motion-toggle {
    display: none !important;
  }
  .office-guide {
    bottom: 80px;
  }
  .office-guide span {
    font-size: 10px;
  }
  .office-notice {
    bottom: 72px;
    left: 12px;
    max-width: calc(100% - 48px);
    font-size: 9px;
  }
  .room-pager {
    font-size: 8px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .panel-enter-active,
  .panel-leave-active {
    transition: none;
  }
}
</style>
