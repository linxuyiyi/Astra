import type { Point3 } from "./layout";

export interface Viewport {
  width: number;
  height: number;
}

export interface ProjectedPoint {
  x: number;
  y: number;
  scale: number;
  /** 旋转后的相机深度，越小越远。 */
  z: number;
  perspective: number;
}

const CAMERA_DISTANCE = 1900;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function baseFit(viewport: Viewport): number {
  return Math.min(viewport.width / 1700, viewport.height / 1000, 1.2);
}

export class NebulaCamera {
  yaw = -0.18;
  pitch = 0.19;
  zoom = 1;
  panX = 0;
  panY = 0;
  target: Point3 = { x: 0, y: 0, z: 0 };

  private targetGoal: Point3 = { x: 0, y: 0, z: 0 };
  private zoomGoal = 1;
  private yawGoal = -0.18;
  private pitchGoal = 0.19;
  private panGoalX = 0;
  private panGoalY = 0;
  private focusId: string | null = null;
  private orbitPivot: Point3 | null = null;
  private orbitAnchor: { x: number; y: number } | null = null;

  get focusedId(): string | null {
    return this.focusId;
  }

  get focused(): boolean {
    return this.focusId !== null;
  }

  project(point: Point3, viewport: Viewport): ProjectedPoint {
    const rotated = this.rotated(point);
    const perspective = clamp(
      CAMERA_DISTANCE / (CAMERA_DISTANCE - rotated.z * 1.2),
      0.42,
      1.9,
    );
    const fit = this.zoom * baseFit(viewport);
    return {
      x: viewport.width * 0.5 + this.panX + rotated.x * fit * perspective,
      y: viewport.height * 0.535 + this.panY + rotated.y * fit * perspective,
      scale: fit * perspective,
      z: rotated.z,
      perspective,
    };
  }

  /** 把屏幕点反投影到相机目标所在的视平面，作为拖拽旋转的临时轴心。 */
  unproject(screenX: number, screenY: number, viewport: Viewport): Point3 {
    const fit = this.zoom * baseFit(viewport);
    const rotatedX = (screenX - viewport.width * 0.5 - this.panX) / fit;
    const rotatedY = (screenY - viewport.height * 0.535 - this.panY) / fit;
    const y = rotatedY * Math.cos(this.pitch);
    const rotatedZ = -rotatedY * Math.sin(this.pitch);
    const x = rotatedX * Math.cos(this.yaw) - rotatedZ * Math.sin(this.yaw);
    const z = rotatedX * Math.sin(this.yaw) + rotatedZ * Math.cos(this.yaw);
    return {
      x: x + this.target.x,
      y: y + this.target.y,
      z: z + this.target.z,
    };
  }

  beginOrbit(screenX: number, screenY: number, viewport: Viewport): void {
    this.focusId = null;
    this.orbitAnchor = { x: screenX, y: screenY };
    this.orbitPivot = this.unproject(screenX, screenY, viewport);
  }

  endOrbit(): void {
    this.orbitPivot = null;
    this.orbitAnchor = null;
  }

  /** 围绕按下时的鼠标点旋转：旋转后重新平移，使该屏幕点保持不动。 */
  orbitBy(deltaX: number, deltaY: number, viewport: Viewport): void {
    if (!this.orbitPivot || !this.orbitAnchor) {
      this.rotate(deltaX, deltaY);
      return;
    }
    this.yaw += deltaX * 0.0022;
    this.pitch = clamp(this.pitch + deltaY * 0.002, -0.48, 0.48);
    this.yawGoal = this.yaw;
    this.pitchGoal = this.pitch;
    this.keepOrbitAnchor(viewport);
  }

  private keepOrbitAnchor(viewport: Viewport): void {
    if (!this.orbitPivot || !this.orbitAnchor) return;
    const rotated = this.rotated(this.orbitPivot);
    const perspective = clamp(
      CAMERA_DISTANCE / (CAMERA_DISTANCE - rotated.z * 1.2),
      0.42,
      1.9,
    );
    const fit = this.zoom * baseFit(viewport);
    this.panX = this.orbitAnchor.x - (viewport.width * 0.5 + rotated.x * fit * perspective);
    this.panY = this.orbitAnchor.y - (viewport.height * 0.535 + rotated.y * fit * perspective);
    this.panGoalX = this.panX;
    this.panGoalY = this.panY;
  }

  private rotated(point: Point3): { x: number; y: number; z: number } {
    const x = point.x - this.target.x;
    const y = point.y - this.target.y;
    const z = point.z - this.target.z;
    const cosYaw = Math.cos(this.yaw);
    const sinYaw = Math.sin(this.yaw);
    const rotatedX = x * cosYaw + z * sinYaw;
    const rotatedZ = -x * sinYaw + z * cosYaw;
    const cosPitch = Math.cos(this.pitch);
    const sinPitch = Math.sin(this.pitch);
    return {
      x: rotatedX,
      y: y * cosPitch - rotatedZ * sinPitch,
      z: y * sinPitch + rotatedZ * cosPitch,
    };
  }

  focusOn(id: string, point: Point3, viewport: Viewport, radius = 190): void {
    this.focusId = id;
    this.endOrbit();
    this.targetGoal = { ...point };
    const base = baseFit(viewport);
    const wanted = Math.min(viewport.width, viewport.height) * 0.3;
    this.zoomGoal = clamp(wanted / Math.max(1, radius * base * 1.15), 1, 3.2);
    this.panGoalX = 0;
    this.panGoalY = 0;
  }

  clearFocus(): void {
    this.focusId = null;
  }

  reset(target: Point3 = { x: 0, y: 0, z: 0 }, zoom = 1): void {
    this.focusId = null;
    this.endOrbit();
    this.targetGoal = { ...target };
    this.zoomGoal = zoom;
    this.yawGoal = -0.18;
    this.pitchGoal = 0.19;
    this.panGoalX = 0;
    this.panGoalY = 0;
  }

  home(target: Point3, radius: number, viewport: Viewport): number {
    const zoom = this.overviewZoom(radius, viewport);
    this.reset(target, zoom);
    return zoom;
  }

  overviewZoom(radius: number, viewport: Viewport): number {
    const base = baseFit(viewport);
    const wanted = Math.min(viewport.width, viewport.height) * 0.42;
    return clamp(wanted / Math.max(1, radius * base * 1.1), 1, 1.32);
  }

  rotate(deltaX: number, deltaY: number): void {
    this.focusId = null;
    this.yawGoal += deltaX * 0.0022;
    this.pitchGoal = clamp(this.pitchGoal + deltaY * 0.002, -0.48, 0.48);
  }

  zoomBy(factor: number): void {
    this.zoomGoal = clamp(this.zoomGoal * factor, 0.68, 3.4);
  }

  panBy(deltaX: number, deltaY: number): void {
    this.panGoalX = this.panX + deltaX;
    this.panGoalY = this.panY + deltaY;
  }

  cruise(deltaSeconds: number): void {
    if (this.focusId) return;
    this.yawGoal += deltaSeconds * 0.055;
    this.pitchGoal = -0.13 + Math.sin(this.yawGoal * 0.9) * 0.06;
  }

  tick(deltaSeconds: number, reducedMotion: boolean): void {
    if (reducedMotion) {
      this.target = { ...this.targetGoal };
      this.zoom = this.zoomGoal;
      this.yaw = this.yawGoal;
      this.pitch = this.pitchGoal;
      this.panX = this.panGoalX;
      this.panY = this.panGoalY;
      return;
    }
    const blend = 1 - Math.exp(-Math.min(0.2, deltaSeconds) * 5.2);
    this.target.x += (this.targetGoal.x - this.target.x) * blend;
    this.target.y += (this.targetGoal.y - this.target.y) * blend;
    this.target.z += (this.targetGoal.z - this.target.z) * blend;
    this.zoom += (this.zoomGoal - this.zoom) * blend;
    this.yaw += (this.yawGoal - this.yaw) * blend;
    this.pitch += (this.pitchGoal - this.pitch) * blend;
    this.panX += (this.panGoalX - this.panX) * blend;
    this.panY += (this.panGoalY - this.panY) * blend;
  }
}
