<template>
  <div
    ref="stage"
    class="office-scene"
    :class="{
      'reduce-motion': reducedMotion,
      'multiple-rows': employees.length > 3,
      'camera-ready': hasInteracted,
    }"
    @click.self="$emit('select', null)"
  >
    <canvas
      ref="backdrop"
      class="backdrop"
      aria-hidden="true"
      @click="$emit('select', null)"
    />
    <div class="stations" :style="worldStyle">
      <div
        v-for="(employee, index) in employees"
        :key="employee.id"
        class="station"
        :class="[
          states[index],
          {
            selected: employee.id === selectedId,
            completed: flashes[employee.id],
          },
        ]"
        :style="{
          left: `${positions[index]!.x}px`,
          top: `${positions[index]!.y}px`,
        }"
      >
        <img class="chair" src="/office/chair.png" alt="" draggable="false" />
        <img class="desk" src="/office/desk.png" alt="" draggable="false" />
        <img
          class="desk-plant"
          src="/office/plant2.png"
          alt=""
          draggable="false"
        />
        <img
          class="robot robot-a"
          :src="employeeAppearance(employee.avatar)?.office.frames[0]"
          :style="characterStyle(employee.avatar)"
          alt=""
          draggable="false"
        />
        <img
          class="robot robot-b"
          :src="employeeAppearance(employee.avatar)?.office.frames[1]"
          :style="characterStyle(employee.avatar)"
          alt=""
          draggable="false"
        />
        <span v-if="states[index] === 'attention'" class="speech"
          >需要关注 ·
          {{ employee.counts.waiting ? "待人工处理" : "执行异常" }}</span
        >
        <span v-else-if="flashes[employee.id]" class="speech success"
          >任务完成</span
        >
        <button
          class="station-hit"
          type="button"
          :aria-pressed="employee.id === selectedId"
          :aria-label="`${employee.name}，${employee.domain || '数字员工'}，${stationLabels[index]}，打开工作台`"
          @click.stop="$emit('select', employee.id)"
        >
          <span class="nameplate"
            ><b>{{ employee.name }}</b
            ><small><i />{{ stationLabels[index] }}</small></span
          >
        </button>
      </div>
    </div>
    <div class="wall-board" :style="wallBoardStyle">
      <RouterLink to="/tasks?status=PENDING" class="board-column queue"
        ><span
          >待分发 <b>{{ snapshot?.totals.tasksQueued ?? "—" }}</b></span
        ><small>{{
          snapshot?.queuedTasks[0]?.title ||
          (snapshot ? "暂无排队任务" : "正在读取工作计划")
        }}</small></RouterLink
      >
      <RouterLink to="/tasks?status=RUNNING" class="board-column running"
        ><span
          >执行中 <b>{{ snapshot?.totals.tasksRunning ?? "—" }}</b></span
        ><small>{{
          snapshot
            ? `${snapshot.totals.tasksRunning} 项任务 · 打开任务大厅`
            : "等待确认运行状态"
        }}</small></RouterLink
      >
      <RouterLink to="/interventions" class="board-column attention"
        ><span
          >待关注
          <b>{{
            snapshot
              ? snapshot.totals.tasksWaiting
              : "—"
          }}</b></span
        ><small>{{
          snapshot?.attentionTasks[0]?.title ||
          (snapshot ? "暂无待处理事项" : "正在读取人工事项")
        }}</small></RouterLink
      >
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { employeeAppearance } from "../../../../shared/employeeAvatar.ts";
import type { NebulaEmployee, NebulaSnapshot } from "../nebula/types";
import {
  employeeState,
  officeCamera,
  officeWallLayout,
  stationPositions,
  STATE_TEXT,
} from "./model";
const props = defineProps<{
  employees: NebulaEmployee[];
  snapshot: NebulaSnapshot | null;
  selectedId: string | null;
  reducedMotion: boolean;
  flashes: Record<string, boolean>;
}>();
defineEmits<{ select: [id: string | null] }>();
const stage = ref<HTMLDivElement>();
const backdrop = ref<HTMLCanvasElement>();
const width = ref(1280),
  height = ref(720);
const hasInteracted = ref(false);
watch(
  () => props.selectedId,
  () => {
    hasInteracted.value = true;
  },
);
const positions = computed(() =>
  stationPositions(props.employees.length, width.value),
);
const degraded = computed(
  () =>
    props.snapshot?.degradedSources.some(
      (s) => s === "tasks" || s === "workers",
    ) ?? true,
);
const states = computed(() =>
  props.employees.map((employee) => employeeState(employee, degraded.value)),
);
const stationLabels = computed(() =>
  props.employees.map((employee, index) =>
    props.flashes[employee.id] &&
    ["idle", "working"].includes(states.value[index]!)
      ? "任务完成"
      : STATE_TEXT[states.value[index]!],
  ),
);
const camera = computed(() =>
  officeCamera(
    width.value,
    height.value,
    props.employees.length,
    !!props.selectedId,
  ),
);
const wallLayout = computed(() =>
  officeWallLayout(width.value, camera.value.wallHeight),
);
const wallBoardStyle = computed(() => {
  if (!wallLayout.value.aspectScaled) {
    return { height: `${camera.value.wallHeight}px` };
  }
  const { offsetX, wallScale } = wallLayout.value;
  return {
    left: `${offsetX + 460 * wallScale}px`,
    width: `${610 * wallScale}px`,
    height: `${camera.value.wallHeight}px`,
    paddingTop: `${56 * wallScale}px`,
    paddingBottom: `${44 * wallScale}px`,
  };
});
const worldStyle = computed(() => ({
  "--label-scale": camera.value.labelScale,
  "--scene-scale": camera.value.scale,
  transform: `translate(${camera.value.x}px, ${camera.value.y}px) scale(${camera.value.scale})`,
}));
function characterStyle(avatar: string) {
  const pose = employeeAppearance(avatar)?.office;
  return pose
    ? {
        left: `${-pose.width / 2}px`,
        top: `${pose.top}px`,
        width: `${pose.width}px`,
        height: `${pose.height}px`,
      }
    : { display: "none" };
}
const images = new Map<string, HTMLImageElement>();
let observer: ResizeObserver | undefined;
// 墙体与地板按 V5 原始场景绘制；家具和机器人使用提取的原 PNG。
function drawBackdrop() {
  const c = backdrop.value;
  if (!c) return;
  const g = c.getContext("2d");
  if (!g) return;
  const w = width.value,
    h = height.value,
    dpr = Math.min(devicePixelRatio || 1, 2);
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  g.scale(dpr, dpr);
  g.imageSmoothingEnabled = false;
  const rect = (
    x: number,
    y: number,
    rw: number,
    rh: number,
    color: string,
  ) => {
    g.fillStyle = color;
    g.fillRect(Math.round(x), Math.round(y), Math.round(rw), Math.round(rh));
  };
  const text = (
    x: number,
    y: number,
    value: string,
    color: string,
    size: number,
    weight = 700,
  ) => {
    g.fillStyle = color;
    g.font = `${weight} ${size}px "PingFang SC",sans-serif`;
    g.fillText(value, x, y);
  };
  const sprite = (
    key: string,
    x: number,
    y: number,
    iw: number,
    ih: number,
  ) => {
    const image = images.get(key);
    if (image?.complete && image.naturalWidth) g.drawImage(image, x, y, iw, ih);
  };
  rect(0, 0, w, h, "#eceef2");
  const floorScale = w > 1920 ? Math.min(1.25, w / 1480) : w / 1480;
  const floorWidth = w / floorScale;
  const floorHeightLogical = h / floorScale;
  g.save();
  g.scale(floorScale, floorScale);
  for (let y = 0, row = 0; y < floorHeightLogical; y += 40, row++) {
    for (let x = -112; x < floorWidth + 112; x += 112) {
      const xx = x + (row % 2) * 56;
      rect(
        xx,
        y + 1,
        110,
        38,
        ["#eaedf1", "#eef0f4", "#e7eaef", "#eff1f5", "#ebedf2"][
          (row * 13 + ((x + 112) / 112) * 7) % 5
        ]!,
      );
      rect(xx, y + 1, 109, 1, "#f8f9fb");
      rect(xx + 109, y + 2, 1, 35, "#d8dce3");
      rect(xx + 9, y + 13, 72, 1, "#cfd4dd66");
      rect(xx + 20, y + 27, 54, 1, "#f4f6f9");
    }
  }
  const floorY = camera.value.wallHeight / floorScale;
  sprite("bookshelf", 45, floorY + 25, 120, 148);
  sprite("plant1", floorWidth - 144, floorY + 85, 57, 91);
  sprite("cabinet", floorWidth - 165, floorY + 215, 98, 80);
  const bottomY = floorHeightLogical;
  if (h > 480 && w > 700) {
    g.save();
    g.translate(98, bottomY - 160);
    g.rotate(-Math.PI / 2);
    sprite("sofa", -93, -60, 186, 120);
    g.restore();
    sprite("coffee", 155, bottomY - 120, 66, 49);
    sprite("plant3", 45, bottomY - 100, 56, 89);
    sprite("plant2", floorWidth - 140, bottomY - 110, 47, 78);
  }
  g.restore();
  // 固定墙面不跟随工位聚焦；宽屏按墙高与 1480×196 基准等比居中，避免 Logo 和看板被横向拉扁。
  const drawWallWindow = (x: number, seed: number) => {
    rect(x, 25, 136, 133, "#3f3f46");
    rect(x + 5, 30, 126, 121, "#dbeafe");
    rect(x + 5, 32, 126, 53, "#eff6ff");
    rect(x + 5, 87, 126, 62, "#c7ddf7");
    for (let k = 0; k < 6; k++) {
      const ht = 19 + ((k * 9 + seed * 6) % 33);
      rect(x + 9 + k * 19, 148 - ht, 15, ht, k % 2 ? "#94a3b8" : "#cbd5e1");
    }
    rect(x + 67, 30, 4, 119, "#52525b");
    rect(x + 5, 87, 126, 3, "#52525b");
  };
  const { aspectScaled, wallScale, offsetX } = wallLayout.value;
  g.save();
  if (aspectScaled) {
    const wallWidthLogical = w / wallScale;
    const offsetLogical = offsetX / wallScale;
    g.scale(wallScale, wallScale);
    rect(0, 0, wallWidthLogical, 196, "#f4f5f7");
    rect(0, 0, wallWidthLogical, 8, "#18181b");
    rect(0, 8, wallWidthLogical, 3, "#d4d4d8");
    rect(0, 182, wallWidthLogical, 7, "#c7cbd3");
    rect(0, 190, wallWidthLogical, 6, "#e4e6ec");
    const startPanel = offsetLogical % 72;
    for (let x = startPanel - 72; x < wallWidthLogical; x += 72) {
      rect(x + 1, 14, 2, 166, "#e2e5ec99");
      rect(x + 6, 15, 1, 162, "#ffffffaa");
    }
    // 超宽两侧墙面对称补一扇像素窗，避免 2K 边墙空洞
    if (offsetLogical >= 180) {
      drawWallWindow((offsetLogical - 136) / 2, 2);
      drawWallWindow(
        wallWidthLogical - offsetLogical + (offsetLogical - 136) / 2,
        3,
      );
    }
    g.translate(offsetLogical, 0);
  } else {
    g.scale(w / 1480, camera.value.wallHeight / 196);
    rect(0, 0, 1480, 196, "#f4f5f7");
    rect(0, 0, 1480, 8, "#18181b");
    rect(0, 8, 1480, 3, "#d4d4d8");
    rect(0, 182, 1480, 7, "#c7cbd3");
    rect(0, 190, 1480, 6, "#e4e6ec");
    for (let x = 0; x < 1480; x += 72) {
      rect(x + 1, 14, 2, 166, "#e2e5ec99");
      rect(x + 6, 15, 1, 162, "#ffffffaa");
    }
  }
  rect(37, 25, 364, 140, "#d4d8e0");
  rect(43, 22, 354, 137, "#e4e4e7");
  rect(50, 26, 341, 127, "#ffffff");
  rect(62, 41, 5, 99, "#18181b");
  rect(372, 41, 4, 99, "#e4e4e7");
  rect(86, 60, 18, 18, "#18181b");
  rect(102, 49, 13, 15, "#2563eb");
  rect(79, 75, 16, 11, "#d4d4d8");
  text(128, 89, "NEXUS", "#09090b", 43, 900);
  text(130, 117, "数字办公室", "#3f3f46", 17, 700);
  text(130, 137, "DIGITAL WORKSPACE", "#71717a", 10);
  rect(433, 19, 658, 153, "#d4d8e0");
  rect(439, 15, 649, 153, "#e4e4e7");
  rect(446, 22, 636, 138, "#ffffff");
  rect(446, 22, 636, 27, "#f4f4f6");
  rect(454, 28, 10, 11, "#18181b");
  text(470, 39, "工作计划  /  WORK BOARD", "#09090b", 13, 700);
  for (let i = 0; i < 2; i++) {
    drawWallWindow(1120 + i * 153, i);
  }
  g.restore();
  if (w < 760) {
    rect(0, 0, w, camera.value.wallHeight, "#f4f5f7");
    rect(0, 0, w, 6, "#18181b");
    rect(0, camera.value.wallHeight - 6, w, 6, "#c7cbd3");
    text(18, 36, "NEXUS", "#09090b", 22, 900);
    text(18, 53, "数字办公室", "#3f3f46", 10, 700);
    text(w - 72, 37, "工作计划", "#52525b", 10);
  }
  const edge = Math.max(9, (w / 1480) * 19);
  rect(0, camera.value.wallHeight, edge, h, "#d8dce3");
  rect(w - edge, camera.value.wallHeight, edge, h, "#cfd4dd");
}
onMounted(() => {
  for (const key of [
    "bookshelf",
    "sofa",
    "coffee",
    "plant1",
    "plant2",
    "plant3",
    "cabinet",
  ]) {
    const image = new Image();
    images.set(key, image);
    image.onload = drawBackdrop;
    image.src = `/office/${key}.png`;
  }
  observer = new ResizeObserver((entries) => {
    const box = entries[0]!.contentRect;
    width.value = box.width;
    height.value = box.height;
    drawBackdrop();
  });
  if (stage.value) observer.observe(stage.value);
});
watch(() => camera.value.wallHeight, drawBackdrop);
onBeforeUnmount(() => {
  observer?.disconnect();
  for (const image of images.values()) image.onload = null;
});
</script>

<style scoped>
.office-scene {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #eceef2;
  isolation: isolate;
}
.backdrop {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
.stations {
  position: absolute;
  width: 1480px;
  height: 1000px;
  transform-origin: 0 0;
  pointer-events: none;
  transition: none;
}
.camera-ready .stations {
  transition: transform 0.3s ease;
}
.station {
  position: absolute;
  width: 0;
  height: 0;
  --state: var(--astra-success, #15803d);
}
.station::before {
  content: "";
  position: absolute;
  left: -168px;
  top: -106px;
  width: 336px;
  height: 284px;
  background: #e2e5eb;
  border: 7px solid #cfd4dd;
  box-shadow:
    inset 0 0 0 3px #f4f5f8,
    0 5px 0 rgba(15, 23, 42, 0.12);
}
.station img {
  position: absolute;
  image-rendering: pixelated;
  user-select: none;
  pointer-events: none;
}
.chair {
  left: -46px;
  top: 42px;
  width: 93px;
  height: 94px;
}
.desk {
  left: -117px;
  top: -88px;
  width: 236px;
  height: 162px;
}
.desk-plant {
  left: 104px;
  top: -17px;
  width: 35px;
  height: 55px;
}
.robot {
  left: -37px;
  top: 13px;
  width: 74px;
  height: 104px;
}
.robot-b {
  opacity: 0;
}
.working .robot-a {
  animation: frame-a 1.2s steps(1) infinite;
}
.working .robot-b {
  animation: frame-b 1.2s steps(1) infinite;
}
.station-hit {
  pointer-events: auto;
  position: absolute;
  left: -156px;
  top: -95px;
  width: 312px;
  height: 284px;
  background: transparent;
  border: 0;
  cursor: pointer;
  color: var(--astra-text-primary);
  border-radius: 4px;
}
.station-hit:hover,
.station-hit:focus-visible,
.selected .station-hit {
  outline: calc(2px * var(--label-scale)) solid var(--astra-primary);
  outline-offset: 9px;
  background: rgba(255, 255, 255, 0.14);
}
.nameplate {
  position: absolute;
  top: 235px;
  left: 50%;
  transform: translateX(-50%) scale(var(--label-scale));
  transform-origin: top center;
  padding: 5px 10px;
  background: rgba(255, 255, 255, 0.95);
  border: 1px solid #d4d4d8;
  border-radius: 7px;
  box-shadow: 0 3px 0 rgba(9, 9, 11, 0.08);
  white-space: nowrap;
  max-width: calc(280px * var(--scene-scale));
}
.nameplate b {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
  font-weight: 700;
  color: var(--astra-text-primary);
}
.nameplate small {
  display: block;
  margin-top: 3px;
  font-size: 9px;
  color: var(--astra-text-muted);
}
.nameplate i {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  background: var(--state);
}
.attention {
  --state: var(--astra-warning);
}
.completed.working .nameplate,
.completed.idle .nameplate {
  background: #f0fdf4;
  border-color: var(--astra-success);
}
.unknown,
.offline {
  --state: var(--astra-neutral);
}
.offline .robot {
  filter: grayscale(0.7);
}
.speech {
  position: absolute;
  left: 32px;
  top: -146px;
  transform: scale(var(--label-scale));
  transform-origin: bottom left;
  padding: 7px 10px;
  font-size: 12px;
  color: var(--astra-danger);
  background: #fef2f2;
  border-left: 4px solid var(--astra-danger);
  border-radius: 0 6px 6px 0;
  box-shadow: 2px 3px 0 rgba(9, 9, 11, 0.10);
  white-space: nowrap;
}
.multiple-rows .speech {
  display: none;
}
.speech.success {
  color: var(--astra-success);
  background: #f0fdf4;
  border-color: var(--astra-success);
}
.wall-board {
  position: absolute;
  left: 31.1%;
  width: 41.2%;
  top: 0;
  display: flex;
  gap: 10px;
  padding-top: min(6.4vh, 49px);
  padding-bottom: min(5.5vh, 42px);
  box-sizing: border-box;
  pointer-events: none;
}
.board-column {
  flex: 1;
  min-width: 0;
  padding: 7px 8px;
  background: var(--astra-bg-subtle);
  text-decoration: none;
  color: var(--astra-text-secondary);
  border-left: 3px solid var(--astra-info);
  border-radius: 0 4px 4px 0;
  pointer-events: auto;
  transition: background-color 0.15s ease;
}
.board-column:hover {
  background: var(--astra-bg-panel);
}
.board-column span {
  font-size: clamp(9px, 1vw, 12px);
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-weight: 700;
}
.board-column b {
  color: var(--astra-text-primary);
}
.board-column small {
  display: block;
  margin-top: 9px;
  font-size: clamp(8px, 0.8vw, 11px);
  line-height: 1.6;
  color: var(--astra-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.board-column.queue {
  border-color: var(--astra-warning);
}
.board-column.attention {
  border-color: var(--astra-danger);
}
.reduce-motion .stations {
  transition: none;
}
.reduce-motion .robot {
  animation: none;
}
@keyframes frame-a {
  50% {
    opacity: 0;
  }
}
@keyframes frame-b {
  50% {
    opacity: 1;
  }
}
@media (prefers-reduced-motion: reduce) {
  .stations {
    transition: none;
  }
  .robot {
    animation: none !important;
  }
}
@media (min-width: 2240px) {
  .nameplate {
    padding: 6px 12px;
    border-radius: 8px;
  }
  .nameplate b {
    font-size: 13px;
  }
  .nameplate small {
    font-size: 10.5px;
  }
  .wall-board {
    gap: 14px;
  }
  .board-column {
    padding: 10px 12px;
    border-left-width: 4px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .board-column span {
    font-size: 14px;
  }
  .board-column small {
    margin-top: 8px;
    font-size: 12.5px;
    line-height: 1.5;
  }
}
@media (max-width: 759px) {
  .wall-board {
    left: 16px;
    right: 16px;
    width: auto;
    top: 64px;
    height: 36px !important;
    gap: 8px;
    padding: 0;
  }
  .board-column {
    padding: 4px 2px;
    border-left-width: 2px;
  }
  .board-column small {
    display: none;
  }
  .board-column span {
    display: flex;
    font-size: 10px;
  }
  .board-column b {
    margin-top: 0;
  }
}
</style>
