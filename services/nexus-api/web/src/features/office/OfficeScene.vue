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
    <div class="wall-board" :style="{ height: `${camera.wallHeight}px` }">
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
              ? snapshot.totals.tasksWaiting + snapshot.totals.tasksFailed
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
  rect(0, 0, w, h, "#e9ddc5");
  const scale = w / 1480;
  g.save();
  g.scale(scale, scale);
  for (let y = 0, row = 0; y < h / scale; y += 40, row++) {
    for (let x = -112; x < 1592; x += 112) {
      const xx = x + (row % 2) * 56;
      rect(
        xx,
        y + 1,
        110,
        38,
        ["#e8d9bd", "#ecdfc8", "#e5d5b7", "#eddfc4", "#e9dcc2"][
          (row * 13 + ((x + 112) / 112) * 7) % 5
        ]!,
      );
      rect(xx, y + 1, 109, 1, "#f9edda");
      rect(xx + 109, y + 2, 1, 35, "#cebda5");
      rect(xx + 9, y + 13, 72, 1, "#d9c7aa66");
      rect(xx + 20, y + 27, 54, 1, "#f3e8d1");
    }
  }
  const floorY = camera.value.wallHeight / scale;
  sprite("bookshelf", 45, floorY + 25, 120, 148);
  sprite("plant1", 1336, floorY + 85, 57, 91);
  sprite("cabinet", 1315, floorY + 215, 98, 80);
  const bottomY = h / scale;
  if (h > 480 && w > 700) {
    g.save();
    g.translate(98, bottomY - 160);
    g.rotate(-Math.PI / 2);
    sprite("sofa", -93, -60, 186, 120);
    g.restore();
    sprite("coffee", 155, bottomY - 120, 66, 49);
    sprite("plant3", 45, bottomY - 100, 56, 89);
    sprite("plant2", 1340, bottomY - 110, 47, 78);
  }
  g.restore();
  // 固定墙面不跟随工位聚焦，完整品牌和计划看板始终可见。
  g.save();
  g.scale(w / 1480, camera.value.wallHeight / 196);
  rect(0, 0, 1480, 196, "#bcd2bc");
  rect(0, 0, 1480, 11, "#6a987d");
  rect(0, 12, 1480, 3, "#91b69b");
  rect(0, 181, 1480, 8, "#769f83");
  rect(0, 190, 1480, 6, "#d6e5cb");
  for (let x = 0; x < 1480; x += 72) {
    rect(x + 1, 17, 2, 162, "#9bb9a0a0");
    rect(x + 6, 18, 1, 158, "#e0e9d8aa");
  }
  rect(37, 25, 364, 140, "#82a48b");
  rect(43, 22, 354, 137, "#eeeadd");
  rect(50, 26, 341, 127, "#faf8ee");
  rect(62, 41, 5, 99, "#a3c8ad");
  rect(372, 41, 4, 99, "#d2dfcf");
  rect(86, 60, 18, 18, "#3d8566");
  rect(102, 49, 13, 15, "#6aac89");
  rect(79, 75, 16, 11, "#9fc7a5");
  text(128, 89, "NEXUS", "#2e604a", 43, 900);
  text(130, 117, "数字办公室", "#466e58", 17, 800);
  text(130, 137, "DIGITAL WORKSPACE", "#829984", 10);
  rect(433, 19, 658, 153, "#709581");
  rect(439, 15, 649, 153, "#cfdcc9");
  rect(446, 22, 636, 138, "#fff8e9");
  rect(446, 22, 636, 27, "#e9f1df");
  rect(454, 28, 10, 11, "#6fa782");
  text(470, 39, "工作计划  /  WORK BOARD", "#3c6650", 13, 800);
  for (let i = 0; i < 2; i++) {
    const x = 1120 + i * 153;
    rect(x, 25, 136, 133, "#648e7d");
    rect(x + 5, 30, 126, 121, "#d4e9d7");
    rect(x + 5, 32, 126, 53, "#e6f2de");
    rect(x + 5, 87, 126, 62, "#b7d8c6");
    for (let k = 0; k < 6; k++) {
      const ht = 19 + ((k * 9 + i * 6) % 33);
      rect(x + 9 + k * 19, 148 - ht, 15, ht, k % 2 ? "#94b4a8" : "#a7cabc");
    }
    rect(x + 67, 30, 4, 119, "#88ada0");
    rect(x + 5, 87, 126, 3, "#89b3a4");
  }
  g.restore();
  if (w < 760) {
    rect(0, 0, w, camera.value.wallHeight, "#bcd2bc");
    rect(0, 0, w, 7, "#6a987d");
    rect(0, camera.value.wallHeight - 6, w, 6, "#769f83");
    text(18, 36, "NEXUS", "#2e604a", 22, 900);
    text(18, 53, "数字办公室", "#466e58", 10, 800);
    text(w - 72, 37, "工作计划", "#55755b", 10);
  }
  const edge = Math.max(9, (w / 1480) * 19);
  rect(0, camera.value.wallHeight, edge, h, "#89aa93");
  rect(w - edge, camera.value.wallHeight, edge, h, "#80a28a");
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
  background: #e9ddc5;
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
  --state: #6b967b;
}
.station::before {
  content: "";
  position: absolute;
  left: -168px;
  top: -106px;
  width: 336px;
  height: 284px;
  background: #d8e2cf;
  border: 7px solid #c3d3bb;
  box-shadow:
    inset 0 0 0 3px #eaf1dd,
    0 5px 0 #b9cbb780;
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
  color: #365d47;
  border-radius: 2px;
}
.station-hit:hover,
.station-hit:focus-visible,
.selected .station-hit {
  outline: calc(2px * var(--label-scale)) solid #3b8567;
  outline-offset: 9px;
  background: #ffffff0d;
}
.nameplate {
  position: absolute;
  top: 235px;
  left: 50%;
  transform: translateX(-50%) scale(var(--label-scale));
  transform-origin: top center;
  padding: 5px 9px;
  background: #fffdf0f2;
  border: 1px solid #aec4ad;
  box-shadow: 0 3px 0 #91aa8d70;
  white-space: nowrap;
  max-width: calc(280px * var(--scene-scale));
}
.nameplate b {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
  font-weight: 800;
}
.nameplate small {
  display: block;
  margin-top: 3px;
  font-size: 9px;
  color: #59725f;
}
.nameplate i {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  background: var(--state);
}
.attention {
  --state: #b56445;
}
.completed.working .nameplate,
.completed.idle .nameplate {
  background: #f1f8e8;
  border-color: #65a279;
}
.unknown,
.offline {
  --state: #89958b;
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
  color: #9b4436;
  background: #fff0e7;
  border-left: 5px solid #c96e58;
  box-shadow: 2px 3px 0 #8b4b3d66;
  white-space: nowrap;
}
.multiple-rows .speech {
  display: none;
}
.speech.success {
  color: #347254;
  background: #edf7df;
  border-color: #6fa782;
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
  background: #f6f5ea;
  text-decoration: none;
  color: #506b58;
  border-left: 3px solid #6caa89;
  pointer-events: auto;
}
.board-column:hover {
  background: #fffdf6;
}
.board-column span {
  font-size: clamp(9px, 1vw, 12px);
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-weight: 800;
}
.board-column b {
  color: #4d8d68;
}
.board-column small {
  display: block;
  margin-top: 9px;
  font-size: clamp(8px, 0.8vw, 11px);
  line-height: 1.6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.board-column.queue {
  border-color: #d7a06a;
}
.board-column.attention {
  border-color: #bd7a63;
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
