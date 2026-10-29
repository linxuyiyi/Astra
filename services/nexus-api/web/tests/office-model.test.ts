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
test("one to twelve stations fit the overview and the area beside the workbench", () => {
  for (const count of [1, 3, 4, 6, 7, 9, 12]) {
    for (const [w, h] of [
      [1280, 720],
      [1920, 1080],
      [1024, 768],
      [390, 844],
    ]) {
      const stations = stationPositions(count, w);
      assert.equal(new Set(stations.map((p) => `${p.x}:${p.y}`)).size, count);
      const overview = officeCamera(w, h, count);
      assert.ok(
        overview.scale <= 0.78,
        "a small team must not fill the screen with oversized sprites",
      );
      for (const open of [false, true]) {
        const view = officeCamera(w, h, count, open);
        const right =
          w - (open && w >= 760 ? Math.min(440, Math.max(365, w * 0.315)) : 0);
        const bottom = h - 96;
        if (w < 760)
          assert.deepEqual(
            view,
            overview,
            "the mobile sheet must not shrink the office",
          );
        assert.ok(
          view.scale <= overview.scale,
          "opening a workbench must never magnify an employee",
        );
        for (const station of stations) {
          const x = view.x + station.x * view.scale;
          const y = view.y + station.y * view.scale;
          assert.ok(x - 175 * view.scale >= 0 && x + 175 * view.scale <= right);
          assert.ok(y - 160 * view.scale >= view.wallHeight);
          assert.ok(y + 140 * view.scale + 44 <= bottom);
        }
      }
    }
  }
});
test("partial last rows remain centered and twelve employees have distinct accessible stations", () => {
  for (const count of [4, 7, 10, 11, 12]) {
    for (const w of [390, 1280]) {
      const rows = Map.groupBy(
        stationPositions(count, w),
        (station) => station.y,
      );
      for (const stations of rows.values()) {
        assert.equal((stations[0]!.x + stations.at(-1)!.x) / 2, 740);
        for (let i = 1; i < stations.length; i++) {
          assert.ok(
            stations[i]!.x - stations[i - 1]!.x > 350,
            "carpets and selection areas must not overlap",
          );
        }
      }
    }
  }
  assert.deepEqual(stationPositions(0), []);
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
