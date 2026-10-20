import assert from 'node:assert/strict'
import test from 'node:test'

import { NebulaCamera, type Viewport } from '../src/features/nebula/camera.ts'

const viewport: Viewport = { width: 1200, height: 800 }

function close(actual: number, expected: number, tolerance = 0.001): boolean {
  return Math.abs(actual - expected) <= tolerance
}

test('unproject round-trips a screen point on the target plane', () => {
  const camera = new NebulaCamera()
  camera.target = { x: 80, y: -30, z: 60 }
  camera.yaw = 0.24
  camera.pitch = 0.18
  camera.zoom = 1.15
  camera.panX = 45
  camera.panY = -28
  const screen = { x: 420, y: 330 }
  const world = camera.unproject(screen.x, screen.y, viewport)
  const projected = camera.project(world, viewport)
  assert.ok(close(projected.x, screen.x))
  assert.ok(close(projected.y, screen.y))
})

test('dragging keeps the pressed screen point fixed while the camera rotates', () => {
  const camera = new NebulaCamera()
  camera.target = { x: 80, y: -30, z: 60 }
  camera.yaw = 0.24
  camera.pitch = 0.18
  camera.zoom = 1.15
  camera.panX = 45
  camera.panY = -28
  const anchor = { x: 360, y: 470 }
  const pivot = camera.unproject(anchor.x, anchor.y, viewport)
  const before = camera.project(pivot, viewport)
  assert.ok(close(before.x, anchor.x))
  assert.ok(close(before.y, anchor.y))

  const yawBefore = camera.yaw
  const pitchBefore = camera.pitch
  camera.beginOrbit(anchor.x, anchor.y, viewport)
  camera.orbitBy(70, 35, viewport)

  assert.notEqual(camera.yaw, yawBefore)
  assert.notEqual(camera.pitch, pitchBefore)
  const after = camera.project(pivot, viewport)
  assert.ok(close(after.x, anchor.x, 0.01))
  assert.ok(close(after.y, anchor.y, 0.01))
})

test('reset clears the temporary orbit pivot', () => {
  const camera = new NebulaCamera()
  camera.beginOrbit(300, 300, viewport)
  camera.orbitBy(40, 20, viewport)
  camera.reset({ x: 0, y: 0, z: 0 }, 1)
  const before = camera.project({ x: 0, y: 0, z: 0 }, viewport)
  camera.orbitBy(40, 20, viewport)
  const after = camera.project({ x: 0, y: 0, z: 0 }, viewport)
  assert.ok(close(before.x, after.x))
  assert.ok(close(before.y, after.y))
})
