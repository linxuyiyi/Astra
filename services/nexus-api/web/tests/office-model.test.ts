import assert from "node:assert/strict";
import test from "node:test";
import {
  employeeState,
  officeCamera,
  stationPositions,
  taskReason,
} from "../src/features/office/model.ts";
import type {
  NebulaEmployee,
  NebulaTask,
} from "../src/features/nebula/types.ts";
const employee = {
  id: "employee-1",
  domain: "测试自动化工程师",
  description: "",
  enabled: true,
  counts: { running: 1, waiting: 0, failed: 0 },
} as NebulaEmployee;
test("degraded data never presents a normal idle/working employee; waiting overrides task work", () => {
  assert.equal(employeeState(employee, true), "unknown");
  assert.equal(
    employeeState({ ...employee, counts: { ...employee.counts, waiting: 1 } }),
    "attention",
  );
  assert.equal(employeeState({ ...employee, enabled: false }, true), "offline");
});
test("three/six desks remain separate and focus stays in the visible office area", () => {
  for (const count of [3, 6]) {
    const stations = stationPositions(count);
    assert.equal(new Set(stations.map((p) => `${p.x}:${p.y}`)).size, count);
    for (const [w, h] of [
      [1280, 720],
      [1920, 1080],
      [390, 844],
    ]) {
      const overview = officeCamera(w!, h!, count);
      for (const desk of stations) {
        const x = overview.x + desk.x * overview.scale,
          y = overview.y + desk.y * overview.scale;
        assert.ok(x > 0 && x < w!);
        assert.ok(y > overview.wallHeight && y < h! - 60);
      }
      for (let index = 0; index < count; index++) {
        const focus = officeCamera(w!, h!, count, index),
          desk = stations[index]!;
        const x = focus.x + desk.x * focus.scale,
          y = focus.y + desk.y * focus.scale;
        assert.ok(x > 0 && x < (w! < 760 ? w! : w! - 365));
        assert.ok(y > focus.wallHeight && y < (w! < 760 ? h! * 0.44 : h!));
      }
    }
  }
});
test("human issues retain actual reason and unavailable connection is visible", () => {
  assert.equal(
    taskReason({
      status: "WAITING",
      waitingReason: "请授权测试环境",
    } as NebulaTask),
    "请授权测试环境",
  );
  assert.match(
    taskReason({
      status: "RUNNING",
      runtimeConnection: "UNAVAILABLE",
    } as NebulaTask),
    /执行连接不可用/,
  );
});
