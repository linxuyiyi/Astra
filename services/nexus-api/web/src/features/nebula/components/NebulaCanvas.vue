<template>
  <div ref="host" class="nebula-canvas" :class="{ 'is-grabbing': dragging }">
    <canvas
      ref="canvas"
      tabindex="0"
      role="application"
      aria-label="数字员工星系画布。可用方向键旋转，加减号缩放，0 重置视角。"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="endDrag"
      @pointerleave="onPointerLeave"
      @wheel.prevent="onWheel"
      @keydown="onKeyDown"
    />
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { NebulaCamera } from "../camera";
import { NebulaRenderer, type HitRegion, type NebulaSelection, type RenderState } from "../renderer";
import type { NebulaScene, Point3 } from "../layout";

const props = defineProps<{
  scene: NebulaScene | null;
  selectedEmployeeId: string | null;
  selectedWorkerId: string | null;
  selectedTaskId: string | null;
  selectedCollaborationKey: string | null;
  showCollaborations: boolean;
  cruise: boolean;
  reducedMotion: boolean;
  pulses: Record<string, number>;
  flashes: Record<string, number>;
}>();

const emit = defineEmits<{
  select: [selection: NebulaSelection];
}>();

const host = ref<HTMLDivElement | null>(null);
const canvas = ref<HTMLCanvasElement | null>(null);
const dragging = ref(false);
const renderer = new NebulaRenderer();
const camera = new NebulaCamera();
let context: CanvasRenderingContext2D | null = null;
let width = 100;
let height = 100;
let dpr = 1;
let frameHandle = 0;
let lastFrame = 0;
let elapsed = 0;
let hits: HitRegion[] = [];
let hoveredKey: string | null = null;
let home: { target: Point3; zoom: number } = { target: { x: 0, y: 0, z: 0 }, zoom: 1 };
let drag:
  | { pointerId: number; x: number; y: number; moved: boolean }
  | null = null;

function viewport() {
  return { width, height };
}

function resize(): void {
  const element = host.value;
  const target = canvas.value;
  if (!element || !target) return;
  const bounds = element.getBoundingClientRect();
  width = Math.max(320, bounds.width);
  height = Math.max(260, bounds.height);
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  target.width = Math.round(width * dpr);
  target.height = Math.round(height * dpr);
  target.style.width = `${width}px`;
  target.style.height = `${height}px`;
  context = target.getContext("2d");
  context?.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function renderState(): RenderState {
  return {
    selectedEmployeeId: props.selectedEmployeeId,
    selectedWorkerId: props.selectedWorkerId,
    selectedTaskId: props.selectedTaskId,
    selectedCollaborationKey: props.selectedCollaborationKey,
    hoveredKey,
    showCollaborations: props.showCollaborations,
    reducedMotion: props.reducedMotion,
    time: elapsed,
    pulses: props.pulses,
    flashes: props.flashes,
  };
}

function draw(now: number): void {
  frameHandle = 0;
  if (!context || !canvas.value) return;
  const delta = lastFrame ? Math.min(0.08, (now - lastFrame) / 1000) : 0;
  lastFrame = now;
  elapsed += props.reducedMotion ? delta * 0.2 : delta;
  if (props.cruise && !props.reducedMotion) camera.cruise(delta);
  camera.tick(delta, props.reducedMotion);
  hits = renderer.render(context, props.scene, camera, viewport(), renderState());
  frameHandle = window.requestAnimationFrame(draw);
}

function startLoop(): void {
  if (frameHandle) return;
  lastFrame = 0;
  frameHandle = window.requestAnimationFrame(draw);
}

function stopLoop(): void {
  if (frameHandle) window.cancelAnimationFrame(frameHandle);
  frameHandle = 0;
  lastFrame = 0;
}

function updateHover(x: number, y: number): void {
  let found: HitRegion | null = null;
  for (let index = hits.length - 1; index >= 0; index -= 1) {
    const hit = hits[index];
    if (Math.hypot(x - hit.x, y - hit.y) <= hit.radius) {
      found = hit;
      break;
    }
  }
  hoveredKey = found ? `${found.kind}:${found.id}` : null;
  if (canvas.value) canvas.value.style.cursor = found ? "pointer" : drag ? "grabbing" : "grab";
}

function localPoint(event: PointerEvent): { x: number; y: number } {
  const bounds = canvas.value?.getBoundingClientRect();
  return {
    x: event.clientX - (bounds?.left ?? 0),
    y: event.clientY - (bounds?.top ?? 0),
  };
}

function onPointerDown(event: PointerEvent): void {
  if (event.button !== 0) return;
  const point = localPoint(event);
  drag = {
    pointerId: event.pointerId,
    x: point.x,
    y: point.y,
    moved: false,
  };
  dragging.value = true;
  canvas.value?.setPointerCapture(event.pointerId);
  canvas.value?.focus({ preventScroll: true });
}

function onPointerMove(event: PointerEvent): void {
  const point = localPoint(event);
  if (!drag) {
    updateHover(point.x, point.y);
    return;
  }
  const deltaX = point.x - drag.x;
  const deltaY = point.y - drag.y;
  if (!drag.moved && Math.abs(deltaX) + Math.abs(deltaY) > 5) drag.moved = true;
  if (!drag.moved) return;
  if (event.shiftKey) camera.panBy(deltaX, deltaY);
  else camera.rotate(deltaX, deltaY);
  drag.x = point.x;
  drag.y = point.y;
  hoveredKey = null;
}

function onPointerUp(event: PointerEvent): void {
  const point = localPoint(event);
  const wasDrag = drag;
  endDrag();
  if (!wasDrag || wasDrag.moved) return;
  for (let index = hits.length - 1; index >= 0; index -= 1) {
    const hit = hits[index];
    if (Math.hypot(point.x - hit.x, point.y - hit.y) <= hit.radius) {
      emit("select", { kind: hit.kind, id: hit.id });
      return;
    }
  }
  emit("select", { kind: "empty", id: "" });
}

function endDrag(): void {
  if (drag && canvas.value?.hasPointerCapture(drag.pointerId)) {
    canvas.value.releasePointerCapture(drag.pointerId);
  }
  drag = null;
  dragging.value = false;
  if (canvas.value) canvas.value.style.cursor = hoveredKey ? "pointer" : "grab";
}

function onPointerLeave(): void {
  if (!drag) {
    hoveredKey = null;
    if (canvas.value) canvas.value.style.cursor = "grab";
  }
}

function onWheel(event: WheelEvent): void {
  camera.zoomBy(event.deltaY < 0 ? 1.09 : 0.92);
}

function onKeyDown(event: KeyboardEvent): void {
  const step = event.shiftKey ? 40 : 18;
  switch (event.key) {
    case "ArrowLeft":
      camera.rotate(-step, 0);
      break;
    case "ArrowRight":
      camera.rotate(step, 0);
      break;
    case "ArrowUp":
      camera.rotate(0, -step);
      break;
    case "ArrowDown":
      camera.rotate(0, step);
      break;
    case "+":
    case "=":
      camera.zoomBy(1.12);
      break;
    case "-":
    case "_":
      camera.zoomBy(0.9);
      break;
    case "0":
      resetView();
      break;
    default:
      return;
  }
  event.preventDefault();
}

function computeHome(scene: NebulaScene): { target: Point3; zoom: number } {
  const total = scene.galaxies.length;
  if (!total) return { target: { x: 0, y: 0, z: 0 }, zoom: 1 };
  const center = scene.galaxies.reduce(
    (accumulator, galaxy) => ({
      x: accumulator.x + galaxy.position.x / total,
      y: accumulator.y + galaxy.position.y / total,
      z: accumulator.z + galaxy.position.z / total,
    }),
    { x: 0, y: 0, z: 0 },
  );
  return {
    target: center,
    zoom: camera.overviewZoom(scene.radius, viewport()),
  };
}

function resetView(): void {
  camera.reset(home.target, home.zoom);
}

function focusSelected(): void {
  const id = props.selectedEmployeeId;
  if (!id || !props.scene) return;
  const galaxy = props.scene.galaxyById.get(id);
  if (!galaxy) return;
  camera.focusOn(id, galaxy.position, viewport(), 190);
}

watch(
  () => props.selectedEmployeeId,
  (id) => {
    if (!id) {
      camera.reset(home.target, home.zoom);
      return;
    }
    focusSelected();
  },
);
let seenScene: NebulaScene | null = null;
watch(
  () => props.scene,
  (scene) => {
    if (!scene) return;
    if (!seenScene) {
      seenScene = scene;
      home = computeHome(scene);
      if (props.selectedEmployeeId) focusSelected();
      else camera.home(home.target, scene.radius, viewport());
    } else {
      seenScene = scene;
      if (!props.selectedEmployeeId) home = computeHome(scene);
    }
  },
);
watch(
  () => props.cruise,
  (enabled) => {
    if (enabled) camera.clearFocus();
  },
);

function onVisibilityChange(): void {
  if (document.hidden) stopLoop();
  else startLoop();
}

let observer: ResizeObserver | null = null;

onMounted(() => {
  resize();
  observer = new ResizeObserver(() => {
    resize();
    if (seenScene) home = computeHome(seenScene);
    if (props.selectedEmployeeId) focusSelected();
  });
  if (host.value) observer.observe(host.value);
  document.addEventListener("visibilitychange", onVisibilityChange);
  startLoop();
});

onBeforeUnmount(() => {
  stopLoop();
  observer?.disconnect();
  document.removeEventListener("visibilitychange", onVisibilityChange);
});

defineExpose({ resetView, focusSelected, zoomIn: () => camera.zoomBy(1.16), zoomOut: () => camera.zoomBy(0.87) });
</script>

<style scoped>
.nebula-canvas {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #031a1a;
}
.nebula-canvas canvas {
  display: block;
  width: 100%;
  height: 100%;
  outline: none;
  cursor: grab;
  touch-action: none;
}
.nebula-canvas canvas:focus-visible {
  box-shadow: inset 0 0 0 2px #35b98f;
}
.nebula-canvas.is-grabbing canvas {
  cursor: grabbing;
}
</style>
