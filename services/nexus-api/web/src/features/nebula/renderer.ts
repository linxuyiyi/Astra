import { NebulaCamera, type ProjectedPoint, type Viewport } from "./camera";
import {
  STATUS_COLOR,
  bezierPath,
  bezierPoint,
  hash32,
  type CollaborationEdgeNode,
  type ExecutionEdgeNode,
  type GalaxyNode,
  type NebulaScene,
  type Point3,
  type TaskSatelliteNode,
  type WorkerNode,
} from "./layout";

export interface NebulaSelection {
  kind: "employee" | "worker" | "task" | "execution" | "collaboration" | "empty";
  id: string;
}

export interface HitRegion {
  kind: NebulaSelection["kind"];
  id: string;
  x: number;
  y: number;
  radius: number;
}

export interface RenderState {
  selectedEmployeeId: string | null;
  selectedWorkerId: string | null;
  selectedTaskId: string | null;
  selectedCollaborationKey: string | null;
  hoveredKey: string | null;
  showCollaborations: boolean;
  reducedMotion: boolean;
  time: number;
  pulses: Record<string, number>;
  flashes: Record<string, number>;
}

interface DiskParticle {
  angle: number;
  radius: number;
  height: number;
  bright: number;
  size: number;
  twinkle: number;
}

interface Star {
  x: number;
  y: number;
  z: number;
  radius: number;
  frequency: number;
}

interface LabelBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const HEALTH_COLOR: Record<string, string> = {
  online: "#69e7c4",
  idle: "#9acabc",
  offline: "#687f7b",
  error: "#fa7795",
  unknown: "#a7b9b4",
};

function rgba(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const red = parseInt(value.slice(0, 2), 16);
  const green = parseInt(value.slice(2, 4), 16);
  const blue = parseInt(value.slice(4, 6), 16);
  return `rgba(${red},${green},${blue},${alpha})`;
}

function mulberry32(seed: number): () => number {
  let value = seed >>> 0;
  return () => {
    value += 0x6d2b79f5;
    let next = value;
    next = Math.imul(next ^ (next >>> 15), next | 1);
    next ^= next + Math.imul(next ^ (next >>> 7), next | 61);
    return ((next ^ (next >>> 14)) >>> 0) / 4294967296;
  };
}

export class NebulaRenderer {
  private stars: Star[] = [];
  private dust = new Map<string, DiskParticle[]>();
  private labels: LabelBox[] = [];
  private hits: HitRegion[] = [];
  private width = 100;
  private height = 100;
  private camera = new NebulaCamera();

  constructor() {
    const random = mulberry32(20261021);
    this.stars = Array.from({ length: 280 }, () => ({
      x: random() * 2 - 1,
      y: random() * 2 - 1,
      z: random(),
      radius: random() * 1.3 + 0.25,
      frequency: random() * 2 + 0.4,
    }));
  }

  render(
    context: CanvasRenderingContext2D,
    scene: NebulaScene | null,
    camera: NebulaCamera,
    viewport: Viewport,
    state: RenderState,
  ): HitRegion[] {
    this.camera = camera;
    this.width = viewport.width;
    this.height = viewport.height;
    this.labels = [];
    this.hits = [];
    this.drawAmbient(context, state);
    this.drawStars(context, camera, state);
    if (!scene) return this.hits;

    for (const galaxy of scene.galaxies) this.drawHalo(context, galaxy, state);
    for (const galaxy of scene.galaxies) {
      this.drawOrbit(context, galaxy, 118, 0.21, state, galaxy.employee.id === state.selectedEmployeeId);
      this.drawOrbit(context, galaxy, 144, -0.4, state, false);
      this.drawDust(context, galaxy, state);
    }
    if (state.showCollaborations) {
      for (const edge of scene.collaborationEdges) this.drawCollaboration(context, edge, state);
    }
    for (const edge of scene.executionEdges) this.drawExecution(context, edge, scene, state);
    for (const satellite of scene.satellites) this.drawTask(context, satellite, state);

    const entities: Array<{ depth: number; draw: () => void }> = [];
    for (const worker of scene.workers) {
      const point = camera.project(worker.position, viewport);
      const selectedGalaxy = state.selectedEmployeeId === worker.employeeId;
      entities.push({
        depth: point.z + (selectedGalaxy ? 400 : 0),
        draw: () => this.drawWorker(context, worker, point, scene, state),
      });
    }
    for (const galaxy of scene.galaxies) {
      const point = camera.project(galaxy.position, viewport);
      const selected = state.selectedEmployeeId === galaxy.employee.id;
      entities.push({
        depth: point.z + (selected ? 1000 : 8),
        draw: () => this.drawGalaxy(context, galaxy, point, state),
      });
    }
    entities.sort((left, right) => left.depth - right.depth);
    for (const entity of entities) entity.draw();

    this.drawVignette(context);
    this.drawFlashes(context, scene, state);
    return this.hits;
  }

  private drawAmbient(context: CanvasRenderingContext2D, state: RenderState): void {
    const width = this.width;
    const height = this.height;
    const background = context.createRadialGradient(
      width * 0.51,
      height * 0.48,
      8,
      width * 0.5,
      height * 0.52,
      Math.max(width, height) * 0.86,
    );
    background.addColorStop(0, "#0c4239");
    background.addColorStop(0.46, "#052923");
    background.addColorStop(1, "#010e16");
    context.fillStyle = background;
    context.fillRect(0, 0, width, height);
    const glow = context.createRadialGradient(
      width * 0.44,
      height * 0.5,
      0,
      width * 0.44,
      height * 0.5,
      width * 0.62,
    );
    glow.addColorStop(0, "#1c8f6728");
    glow.addColorStop(0.45, "#06615810");
    glow.addColorStop(1, "#002b3100");
    context.fillStyle = glow;
    context.fillRect(0, 0, width, height);
    if (state.reducedMotion) return;
    context.save();
    context.globalAlpha = 0.12;
    context.strokeStyle = "#8fe6c8";
    context.lineWidth = 0.6;
    const drift = state.time * 3;
    for (let index = 0; index < 14; index += 1) {
      const x = ((index * 137 + drift) % (width + 240)) - 120;
      const y = (index * 97) % height;
      context.beginPath();
      context.moveTo(x, y);
      context.lineTo(x + 70, y - 18);
      context.stroke();
    }
    context.restore();
  }

  private drawStars(
    context: CanvasRenderingContext2D,
    camera: NebulaCamera,
    state: RenderState,
  ): void {
    const width = this.width;
    const height = this.height;
    for (const star of this.stars) {
      const x = (star.x * width) / 2 + width / 2 + camera.yaw * 42 + camera.panX * 0.04;
      const y = (star.y * height) / 2 + height / 2 + camera.pitch * 40 + camera.panY * 0.04;
      if (x < -10 || x > width + 10 || y < -10 || y > height + 10) continue;
      const alpha = state.reducedMotion
        ? 0.2 + star.z * 0.24
        : 0.18 + 0.16 * Math.sin(state.time * star.frequency + star.z * 9);
      context.globalAlpha = alpha;
      context.fillStyle = "#b9fff0";
      context.beginPath();
      context.arc(x, y, Math.max(0.2, star.radius * (1 + star.z / 2)), 0, Math.PI * 2);
      context.fill();
    }
    context.globalAlpha = 1;
  }

  private drawHalo(
    context: CanvasRenderingContext2D,
    galaxy: GalaxyNode,
    state: RenderState,
  ): void {
    const point = this.camera.project(galaxy.position, this.viewport());
    const radius = Math.max(26, 148 * point.scale);
    const selected = galaxy.employee.id === state.selectedEmployeeId;
    const gradient = context.createRadialGradient(point.x, point.y, 6, point.x, point.y, radius * 1.35);
    gradient.addColorStop(0, rgba(galaxy.dust, selected ? 0.17 : 0.09));
    gradient.addColorStop(0.33, rgba(galaxy.color, 0.06));
    gradient.addColorStop(0.66, rgba(galaxy.color, 0.024));
    gradient.addColorStop(1, rgba(galaxy.color, 0));
    context.fillStyle = gradient;
    context.beginPath();
    context.arc(point.x, point.y, radius * 1.35, 0, Math.PI * 2);
    context.fill();
  }

  private drawOrbit(
    context: CanvasRenderingContext2D,
    galaxy: GalaxyNode,
    radius: number,
    phase: number,
    state: RenderState,
    selected: boolean,
  ): void {
    const center = this.camera.project(galaxy.position, this.viewport());
    const runs: Array<{ layer: number; points: Array<{ x: number; y: number }> }> = [];
    let currentLayer = -1;
    let current: Array<{ x: number; y: number }> = [];
    const total = 110;
    for (let index = 0; index <= total; index += 1) {
      const angle = (index / total) * Math.PI * 2 + phase;
      const world: Point3 = {
        x: galaxy.position.x + Math.cos(angle) * radius,
        y: galaxy.position.y + Math.sin(angle) * radius * 0.72,
        z: galaxy.position.z + Math.sin(angle + 0.33) * radius * 0.72,
      };
      const point = this.camera.project(world, this.viewport());
      const layer = point.z > center.z ? 1 : 0;
      if (layer !== currentLayer) {
        if (current.length > 1) runs.push({ layer: currentLayer, points: current });
        current = [];
        currentLayer = layer;
      }
      current.push({ x: point.x, y: point.y });
    }
    if (current.length > 1) runs.push({ layer: currentLayer, points: current });
    for (const run of runs) {
      context.save();
      context.beginPath();
      run.points.forEach((item, index) =>
        index ? context.lineTo(item.x, item.y) : context.moveTo(item.x, item.y),
      );
      context.strokeStyle = rgba(galaxy.color, run.layer ? (selected ? 0.64 : 0.38) : 0.13);
      context.lineWidth = run.layer ? (selected ? 1.7 : 1.1) : 0.75;
      context.shadowColor = galaxy.color;
      context.shadowBlur = run.layer ? 8 : 0;
      context.stroke();
      context.restore();
    }
    void state;
  }

  private drawDust(
    context: CanvasRenderingContext2D,
    galaxy: GalaxyNode,
    state: RenderState,
  ): void {
    const particles = this.ensureDust(galaxy.employee.id);
    const selected = galaxy.employee.id === state.selectedEmployeeId;
    const centerProjection = this.camera.project(galaxy.position, this.viewport());
    for (const particle of particles) {
      const angle = particle.angle + (state.reducedMotion ? 0 : state.time * 0.026);
      const world: Point3 = {
        x: galaxy.position.x + Math.cos(angle) * particle.radius,
        y: galaxy.position.y + particle.height * 0.12 + Math.sin(angle) * particle.radius * 0.3,
        z: galaxy.position.z + particle.height + Math.sin(angle + 0.33) * particle.radius * 0.3,
      };
      const point = this.camera.project(world, this.viewport());
      if (
        point.x < -8 ||
        point.x > this.width + 8 ||
        point.y < -8 ||
        point.y > this.height + 8 ||
        Math.abs(point.x - centerProjection.x) > this.width ||
        Math.abs(point.y - centerProjection.y) > this.height
      ) {
        continue;
      }
      const twinkle = state.reducedMotion
        ? 0.48
        : 0.44 + 0.3 * Math.sin(state.time * 0.9 + particle.twinkle);
      const fade = Math.max(0.1, Math.min(0.86, 0.16 + particle.bright * 0.56 + (selected ? 0.16 : 0)));
      context.globalAlpha = fade * twinkle;
      context.fillStyle = galaxy.dust;
      context.beginPath();
      context.arc(
        point.x,
        point.y,
        Math.min(1.9, 0.38 + particle.size * 0.62) * Math.max(0.55, point.scale),
        0,
        Math.PI * 2,
      );
      context.fill();
    }
    context.globalAlpha = 1;
  }

  private drawCollaboration(
    context: CanvasRenderingContext2D,
    edge: CollaborationEdgeNode,
    state: RenderState,
  ): void {
    const active = (state.pulses[edge.key] ?? 0) > Date.now();
    const selected = state.selectedCollaborationKey === edge.key;
    const hovered = state.hoveredKey === `collaboration:${edge.key}`;
    const alpha = active || selected ? 0.72 : 0.13 + Math.min(0.2, Math.log1p(edge.edge.requestCount) * 0.04);
    const width = 0.9 + Math.min(2.2, Math.log1p(edge.edge.requestCount) * 0.72);
    const color = active || selected ? STATUS_COLOR.collaborationActive : STATUS_COLOR.collaboration;
    const points = bezierPath(edge.from, edge.control, edge.to);
    this.traceCurve(context, points, color, alpha, selected ? width + 0.8 : width, selected ? 12 : 5, selected);
    if (active && !state.reducedMotion) {
      for (let index = 0; index < 3; index += 1) {
        const unit = ((state.time * 0.18 + index / 3) % 1 + 1) % 1;
        const point = this.camera.project(bezierPoint(edge.from, edge.control, edge.to, unit), this.viewport());
        context.fillStyle = rgba("#91dafa", 0.1);
        context.beginPath();
        context.arc(point.x, point.y, 8, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "#d4ffff";
        context.beginPath();
        context.arc(point.x, point.y, 2.6, 0, Math.PI * 2);
        context.fill();
      }
    }
    const middle = this.camera.project(
      bezierPoint(edge.from, edge.control, edge.to, 0.5),
      this.viewport(),
    );
    if (selected || hovered) {
      this.drawLabel(context, `${edge.edge.requestCount} 次职责请求`, middle.x, middle.y - 12, "#d8fbff", 10);
    }
    this.hits.push({
      kind: "collaboration",
      id: edge.key,
      x: middle.x,
      y: middle.y,
      radius: 14,
    });
  }

  private drawExecution(
    context: CanvasRenderingContext2D,
    edge: ExecutionEdgeNode,
    scene: NebulaScene,
    state: RenderState,
  ): void {
    const worker = scene.workerById.get(edge.workerId);
    const health = worker?.health ?? "unknown";
    const waiting = edge.waiting > 0;
    const running = edge.running > 0;
    const unavailable = edge.unavailable > 0;
    const selected =
      state.hoveredKey === `execution:${edge.key}` ||
      (state.selectedWorkerId === edge.workerId && state.selectedEmployeeId === edge.employeeId);
    let color = waiting ? STATUS_COLOR.waiting : running ? STATUS_COLOR.running : "#4f9e8f";
    if (unavailable) color = "#d8b26a";
    if (health === "offline") color = "#547c78";
    else if (health === "error") color = "#ff8193";
    const points = bezierPath(edge.from, edge.control, edge.to);
    this.traceCurve(
      context,
      points,
      color,
      running || waiting ? 0.58 : 0.16,
      selected ? 2.6 : running || waiting ? 1.8 : 1,
      running || waiting ? 8 : 3,
      waiting || unavailable,
    );
    if (
      running &&
      !unavailable &&
      !state.reducedMotion &&
      health === "online"
    ) {
      const unit = ((state.time * 0.22 + hash32(edge.workerId) / 4294967296) % 1 + 1) % 1;
      const point = this.camera.project(bezierPoint(edge.from, edge.control, edge.to, unit), this.viewport());
      context.fillStyle = rgba(color, 0.1);
      context.beginPath();
      context.arc(point.x, point.y, 13, 0, Math.PI * 2);
      context.fill();
      context.fillStyle = rgba(color, 0.75);
      context.beginPath();
      context.arc(point.x, point.y, 4.6, 0, Math.PI * 2);
      context.fill();
      context.fillStyle = "#faffef";
      context.beginPath();
      context.arc(point.x, point.y, 1.9, 0, Math.PI * 2);
      context.fill();
    }
    const middle = this.camera.project(
      bezierPoint(edge.from, edge.control, edge.to, 0.47),
      this.viewport(),
    );
    const total = edge.running + edge.waiting;
    if (unavailable) {
      context.fillStyle = "#f0bd72";
      context.shadowColor = "#f0bd72";
      context.shadowBlur = 9;
      context.beginPath();
      context.arc(middle.x, middle.y - 6, 2.8, 0, Math.PI * 2);
      context.fill();
      context.shadowBlur = 0;
      if (selected) {
        this.drawLabel(context, "状态待确认", middle.x, middle.y - 18, "#ffe0a8", 10, true);
      }
    } else if (running || waiting) {
      const text = waiting
        ? total > 1
          ? `${total} 项任务 · 需人工`
          : "需人工确认"
        : total > 1
          ? `${total} 项任务`
          : "执行中";
      this.drawLabel(context, text, middle.x, middle.y - 10, waiting ? "#ffd1af" : "#bdf5dc", 10);
    }
    this.hits.push({
      kind: "execution",
      id: edge.key,
      x: middle.x,
      y: middle.y,
      radius: Math.max(15, total ? 22 : 12),
    });
  }

  private drawTask(
    context: CanvasRenderingContext2D,
    satellite: TaskSatelliteNode,
    state: RenderState,
  ): void {
    const point = this.camera.project(satellite.position, this.viewport());
    const radius = Math.max(3.6, 6.3 * point.scale);
    const selected = state.selectedTaskId === satellite.task.id;
    const hovered = state.hoveredKey === `task:${satellite.task.id}`;
    context.save();
    context.translate(point.x, point.y);
    context.rotate((state.reducedMotion ? 0 : state.time * 0.15) + satellite.index * 0.5);
    context.fillStyle = selected ? "#ffe3ae" : STATUS_COLOR.queued;
    context.shadowColor = "#e3ab45";
    context.shadowBlur = selected ? 16 : 10;
    context.fillRect(-radius * 0.67, -radius * 0.67, radius * 1.35, radius * 1.35);
    context.restore();
    if (selected || hovered) {
      context.strokeStyle = "rgba(255,231,180,.8)";
      context.lineWidth = 1.2;
      context.beginPath();
      context.arc(point.x, point.y, radius + 6, 0, Math.PI * 2);
      context.stroke();
    }
    this.hits.push({
      kind: "task",
      id: satellite.task.id,
      x: point.x,
      y: point.y,
      radius: 14,
    });
  }

  private drawWorker(
    context: CanvasRenderingContext2D,
    node: WorkerNode,
    point: ProjectedPoint,
    scene: NebulaScene,
    state: RenderState,
  ): void {
    const worker = node.worker;
    const health = worker.health;
    const selected = state.selectedWorkerId === worker.id;
    const hovered = state.hoveredKey === `worker:${worker.id}`;
    const background =
      Boolean(state.selectedEmployeeId) && state.selectedEmployeeId !== node.employeeId;
    const color =
      health === "error"
        ? "#fa7795"
        : health === "offline"
          ? HEALTH_COLOR.offline
          : worker.occupancy === "waiting"
            ? "#ffb37e"
            : worker.occupancy === "running"
              ? HEALTH_COLOR.online
              : HEALTH_COLOR.idle;
    const radius = Math.max(5.5, 14 * point.scale);
    context.save();
    if (background) context.globalAlpha = 0.62;
    this.drawOrb(context, point, radius, color, false, selected, hovered);
    const showName =
      selected ||
      hovered ||
      state.selectedEmployeeId === node.employeeId ||
      (health !== "offline" && health !== "error" && scene.workers.length <= 7) ||
      (health !== "offline" && health !== "error" && point.scale > 1.15);
    if (!background && (health === "offline" || health === "error" || health === "unknown")) {
      this.drawLabel(
        context,
        health === "offline" ? "离线" : health === "error" ? "异常" : "状态未知",
        point.x,
        point.y - radius - 10,
        health === "offline" ? "#a0b4b0" : health === "error" ? "#ff9cb2" : "#d9c28f",
        10,
        true,
      );
    }
    if (showName && !background) {
      this.drawLabel(
        context,
        worker.name,
        point.x,
        point.y + radius + 17,
        selected ? "#eafff7" : "#b7e4d4",
        10,
      );
    }
    context.restore();
    this.hits.push({
      kind: "worker",
      id: worker.id,
      x: point.x,
      y: point.y,
      radius: Math.max(18, radius + 7),
    });
  }

  private drawGalaxy(
    context: CanvasRenderingContext2D,
    galaxy: GalaxyNode,
    point: ProjectedPoint,
    state: RenderState,
  ): void {
    const selected = state.selectedEmployeeId === galaxy.employee.id;
    const hovered = state.hoveredKey === `employee:${galaxy.employee.id}`;
    const background = Boolean(state.selectedEmployeeId) && !selected;
    const color = galaxy.color;
    const radius = Math.max(9, (selected ? 31 : 28) * point.scale);
    context.save();
    if (background) context.globalAlpha = 0.55;
    this.drawOrb(context, point, radius, color, true, selected, hovered);
    const counts = galaxy.employee.counts;
    const summary = `${galaxy.workerIds.length} Worker · ${counts.running} 执行 · ${counts.queued} 排队${
      counts.waiting ? ` · ${counts.waiting} 待人工` : ""
    }${counts.failed ? ` · ${counts.failed} 异常` : ""}`;
    this.drawLabel(
      context,
      galaxy.employee.name,
      point.x,
      point.y + radius + 22,
      background ? "#8fb8a7" : selected ? "#f0fff7" : "#d9f4e8",
      selected ? 13 : 12,
      !background,
    );
    if (!background) {
      this.drawLabel(
        context,
        summary,
        point.x,
        point.y + radius + 39,
        counts.waiting || counts.failed ? "#ffcfac" : "#8ed4b7",
        10,
        selected,
      );
    }
    if (!background && !galaxy.employee.enabled) {
      this.drawLabel(context, "已停用", point.x, point.y - radius - 16, "#b7bdb9", 10, true);
    }
    if (!background && galaxy.queuedCount > galaxy.queue.length) {
      this.drawLabel(
        context,
        `+${galaxy.queuedCount - galaxy.queue.length}`,
        point.x + radius + 18,
        point.y + 5,
        "#ffe1a7",
        10,
        true,
      );
    }
    if (counts.failed > 0) {
      context.fillStyle = STATUS_COLOR.failed;
      context.shadowColor = STATUS_COLOR.failed;
      context.shadowBlur = 8;
      context.beginPath();
      context.arc(point.x + radius + 8, point.y - radius - 4, 3.2, 0, Math.PI * 2);
      context.fill();
      context.shadowBlur = 0;
    }
    context.restore();
    this.hits.push({
      kind: "employee",
      id: galaxy.employee.id,
      x: point.x,
      y: point.y,
      radius: Math.max(30, radius + 8),
    });
  }

  private drawOrb(
    context: CanvasRenderingContext2D,
    point: ProjectedPoint,
    radius: number,
    color: string,
    core: boolean,
    selected: boolean,
    hovered: boolean,
  ): void {
    const glowRadius = radius * (core ? 4.2 : 3.2);
    const glow = context.createRadialGradient(point.x, point.y, 0, point.x, point.y, glowRadius);
    glow.addColorStop(0, rgba(color, core ? 0.45 : 0.3));
    glow.addColorStop(0.24, rgba(color, core ? 0.12 : 0.09));
    glow.addColorStop(1, rgba(color, 0));
    context.fillStyle = glow;
    context.beginPath();
    context.arc(point.x, point.y, glowRadius, 0, Math.PI * 2);
    context.fill();

    const sphere = context.createRadialGradient(
      point.x - radius * 0.34,
      point.y - radius * 0.46,
      radius * 0.12,
      point.x + radius * 0.18,
      point.y + radius * 0.16,
      radius * 1.36,
    );
    sphere.addColorStop(0, "#faffff");
    sphere.addColorStop(0.12, core ? "#d5fff1" : "#dbffff");
    sphere.addColorStop(0.31, color);
    sphere.addColorStop(0.69, core ? "#0b7365" : "#164e52");
    sphere.addColorStop(1, "#021c22");
    context.fillStyle = sphere;
    context.beginPath();
    context.arc(point.x, point.y, radius, 0, Math.PI * 2);
    context.fill();
    context.strokeStyle = rgba(color, 0.9);
    context.lineWidth = core ? 1.5 : 1;
    context.stroke();

    context.save();
    context.beginPath();
    context.arc(point.x, point.y, Math.max(0.5, radius - 0.5), 0, Math.PI * 2);
    context.clip();
    context.strokeStyle = rgba("#ffffff", core ? 0.26 : 0.14);
    context.lineWidth = core ? 2 : 1;
    context.beginPath();
    context.ellipse(
      point.x + radius * 0.27,
      point.y + radius * 0.13,
      radius * 0.77,
      radius * 0.32,
      -0.25,
      0,
      Math.PI * 2,
    );
    context.stroke();
    context.restore();

    const arcRadius = radius * (core ? 1.34 : 1.38);
    context.lineWidth = core ? 1.35 : 1;
    context.strokeStyle = rgba(selected ? "#e4fff4" : color, selected ? 0.95 : 0.48);
    context.beginPath();
    context.arc(point.x, point.y, arcRadius, -0.8, 1.9);
    context.stroke();

    if (core) {
      context.save();
      context.shadowColor = "#bafee2";
      context.shadowBlur = 9;
      context.fillStyle = "#ecfff7";
      context.font = `${Math.max(14, radius * 0.82)}px "Times New Roman", serif`;
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText("✦", point.x, point.y + 1);
      context.restore();
    }
    if (selected || hovered) {
      context.strokeStyle = rgba(color, 0.72);
      context.lineWidth = 1.3;
      context.beginPath();
      context.arc(point.x, point.y, radius + 10, 0, Math.PI * 2);
      context.stroke();
    }
  }

  private drawVignette(context: CanvasRenderingContext2D): void {
    const gradient = context.createRadialGradient(
      this.width * 0.52,
      this.height * 0.53,
      this.width * 0.18,
      this.width * 0.5,
      this.height * 0.5,
      Math.max(this.width, this.height) * 0.76,
    );
    gradient.addColorStop(0, "rgba(0,0,0,0)");
    gradient.addColorStop(0.73, "rgba(0,12,16,.08)");
    gradient.addColorStop(1, "rgba(0,8,13,.56)");
    context.fillStyle = gradient;
    context.fillRect(0, 0, this.width, this.height);
  }

  private drawFlashes(
    context: CanvasRenderingContext2D,
    scene: NebulaScene,
    state: RenderState,
  ): void {
    const now = Date.now();
    for (const [employeeId, until] of Object.entries(state.flashes)) {
      if (until <= now) continue;
      const galaxy = scene.galaxyById.get(employeeId);
      if (!galaxy) continue;
      const point = this.camera.project(galaxy.position, this.viewport());
      const progress = 1 - (until - now) / 4200;
      const radius = Math.max(10, 30 * point.scale) + progress * 70;
      context.strokeStyle = rgba(STATUS_COLOR.completed, 0.7 * (1 - progress));
      context.lineWidth = 2;
      context.shadowColor = STATUS_COLOR.completed;
      context.shadowBlur = 12;
      context.beginPath();
      context.arc(point.x, point.y, radius, 0, Math.PI * 2);
      context.stroke();
      context.shadowBlur = 0;
    }
  }

  private traceCurve(
    context: CanvasRenderingContext2D,
    points: Point3[],
    color: string,
    alpha: number,
    width: number,
    blur: number,
    dashed: boolean,
  ): void {
    if (points.length < 2) return;
    context.save();
    context.globalAlpha = alpha;
    context.beginPath();
    points.forEach((point, index) => {
      const projected = this.camera.project(point, this.viewport());
      if (index === 0) context.moveTo(projected.x, projected.y);
      else context.lineTo(projected.x, projected.y);
    });
    context.lineWidth = width;
    context.strokeStyle = color;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.shadowColor = color;
    context.shadowBlur = blur;
    if (dashed) context.setLineDash([6, 7]);
    context.stroke();
    context.restore();
  }

  private drawLabel(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    color: string,
    size: number,
    priority = false,
  ): void {
    if (!text) return;
    context.save();
    context.font = `${priority ? "600" : "400"} ${size}px Inter, "PingFang SC", "Microsoft YaHei", sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    const metrics = context.measureText(text);
    const half = metrics.width / 2 + 3;
    const box: LabelBox = { left: x - half, top: y - size, right: x + half, bottom: y + size };
    if (!priority && this.labels.some((item) => overlap(item, box))) {
      context.restore();
      return;
    }
    this.labels.push(box);
    context.shadowColor = "#002822";
    context.shadowBlur = 6;
    context.fillStyle = color;
    context.fillText(text, x, y);
    context.restore();
  }

  private viewport(): Viewport {
    return { width: this.width, height: this.height };
  }

  private ensureDust(employeeId: string): DiskParticle[] {
    const cached = this.dust.get(employeeId);
    if (cached) return cached;
    const random = mulberry32(hash32(employeeId));
    const particles = Array.from({ length: 150 }, () => {
      const arm = (Math.floor(random() * 3) * Math.PI * 2) / 3;
      return {
        angle: arm + random() * 5 + (random() - 0.5) * 0.75,
        radius: 20 + Math.sqrt(random()) * 155,
        height: (random() - 0.5) * 92,
        bright: random() * 0.7 + 0.1,
        size: random() * 1.7 + 0.4,
        twinkle: random() * 6.3,
      };
    });
    this.dust.set(employeeId, particles);
    return particles;
  }
}

function overlap(left: LabelBox, right: LabelBox): boolean {
  return !(
    left.right < right.left ||
    left.left > right.right ||
    left.bottom < right.top ||
    left.top > right.bottom
  );
}
