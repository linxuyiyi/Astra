import assert from 'node:assert/strict';
import test from 'node:test';
import type { EmployeeSchedule } from '../src/api.ts';
import {
  buildMonthGrid,
  buildWeekDayTimedEvents,
  buildWeekDays,
  describeCron,
  expandSchedulesForRange,
  parseCronExpression,
  previewNextTriggers,
  restoreFrequencyFromCron,
} from '../src/features/schedules/cron.ts';

function makeSchedule(overrides: {
  id?: string;
  employee_id?: string | null;
  employee_name?: string;
  name?: string;
  cron_expression?: string;
  timezone?: string;
  enabled?: boolean;
}): EmployeeSchedule {
  return {
    id: overrides.id ?? 'sched-1',
    employee_id: overrides.employee_id === undefined ? 'emp-1' : overrides.employee_id,
    employee_name: overrides.employee_name ?? '运营专员',
    owner_uid: 'alice',
    created_at: '2026-11-01T00:00:00Z',
    data: {
      name: overrides.name ?? '工作日晨报',
      description: '汇总当日重点',
      cron_expression: overrides.cron_expression ?? '0 9 * * 1-5',
      timezone: overrides.timezone ?? 'Asia/Shanghai',
      enabled: overrides.enabled ?? true,
      stateful: false,
    },
  };
}

test('parseCronExpression handles steps, ranges, aliases, Sunday 0/7 and rejects invalid syntax', () => {
  const weekdays = parseCronExpression('30 9 * * MON-FRI');
  assert.ok(weekdays);
  assert.deepEqual(weekdays.minutes, [30]);
  assert.deepEqual(weekdays.hours, [9]);
  assert.deepEqual([...weekdays.daysOfWeek].sort(), [1, 2, 3, 4, 5]);

  const sunday7 = parseCronExpression('0 10 * * 7');
  assert.ok(sunday7);
  assert.deepEqual([...sunday7.daysOfWeek], [0]);

  const stepped = parseCronExpression('15/20 8-12/2 * JAN,DEC 5-7');
  assert.ok(stepped);
  assert.deepEqual(stepped.minutes, [15, 35, 55]);
  assert.deepEqual(stepped.hours, [8, 10, 12]);
  assert.deepEqual([...stepped.months].sort((a, b) => a - b), [1, 12]);
  assert.deepEqual([...stepped.daysOfWeek].sort((a, b) => a - b), [0, 5, 6]);

  assert.equal(parseCronExpression('0 9 * *'), null);
  assert.equal(parseCronExpression('60 9 * * *'), null);
  assert.equal(parseCronExpression('0 24 * * *'), null);
  assert.equal(parseCronExpression('0 9 32 * *'), null);
  assert.equal(parseCronExpression('0 9 * * 8'), null);
  assert.equal(parseCronExpression('0 9 5-2 * *'), null);
});

test('describeCron and restoreFrequencyFromCron stay consistent for presets and custom rules', () => {
  assert.equal(describeCron('0 9 * * 1-5').label, '工作日 09:00');
  assert.equal(describeCron('0 * * * *').label, '每小时整点');
  assert.equal(describeCron('0 */2 * * *').label, '每 2 小时');
  assert.equal(describeCron('0 9 1 * *').label, '每月 1 日 09:00');
  assert.equal(describeCron('bad').valid, false);

  assert.deepEqual(restoreFrequencyFromCron('15 8 * * 1-5'), {
    frequency: 'weekdays',
    time: '08:15',
    weekday: 1,
    monthDay: 1,
  });
  assert.deepEqual(restoreFrequencyFromCron('0 18 * * 0'), {
    frequency: 'weekly',
    time: '18:00',
    weekday: 0,
    monthDay: 1,
  });
  assert.deepEqual(restoreFrequencyFromCron('30 9 15 * *'), {
    frequency: 'monthly',
    time: '09:30',
    weekday: 1,
    monthDay: 15,
  });
  assert.equal(restoreFrequencyFromCron('0 */2 * * *').frequency, 'custom');
});

test('buildMonthGrid starts on Monday and buildWeekDays aligns Monday to Sunday', () => {
  // 2026-11-01 是周日，因此周一开头的 11 月网格应从 2026-10-26（周一）开始，共 6 周 42 格
  const cells = buildMonthGrid(2026, 11, '2026-11-26');
  assert.equal(cells[0]!.ymd, '2026-10-26');
  assert.equal(cells[0]!.dow, 1);
  assert.equal(cells[0]!.isCurrentMonth, false);
  assert.equal(cells.find(c => c.ymd === '2026-11-26')?.isToday, true);
  assert.equal(cells.at(-1)!.ymd, '2026-12-06');
  assert.equal(cells.length, 42);

  const week = buildWeekDays('2026-11-26', '2026-11-26');
  assert.equal(week.length, 7);
  assert.equal(week[0]!.ymd, '2026-11-23'); // Monday
  assert.equal(week[6]!.ymd, '2026-11-29'); // Sunday
});

test('expandSchedulesForRange follows APScheduler AND semantics for DOM + DOW and skips non-existent dates', () => {
  // 2026-11-13 是周五，2026-11-14 是周六；0 9 13 * 5 仅在 11月13日匹配
  const friday13 = makeSchedule({ id: 'fri13', name: '黑色星期五检查', cron_expression: '0 9 13 * 5' });
  // 每月 31 日在 11 月（仅 30 天）不应触发
  const day31 = makeSchedule({ id: 'd31', name: '月末31日', cron_expression: '0 10 31 * *' });

  const map = expandSchedulesForRange([friday13, day31], '2026-11-01', '2026-11-30', 'Asia/Shanghai');
  assert.equal(map.size, 1);
  assert.equal(map.get('2026-11-13')?.length, 1);
  assert.equal(map.get('2026-11-13')![0]!.times[0], '09:00');
});

test('expandSchedulesForRange accurately projects cross-timezone schedules across day boundaries', () => {
  // UTC 20:30 映射到 Asia/Shanghai (+8h) 应落在次日 04:30
  const utcLate = makeSchedule({
    id: 'utc-late',
    name: 'UTC 晚间汇总',
    cron_expression: '30 20 * * 1', // UTC 周一 20:30 -> 上海 周二 04:30
    timezone: 'UTC',
  });
  const map = expandSchedulesForRange([utcLate], '2026-11-23', '2026-11-29', 'Asia/Shanghai');
  // 2026-11-23 是周一，映射后应在周二 2026-11-24 04:30
  assert.equal(map.get('2026-11-23'), undefined);
  const tuesday = map.get('2026-11-24');
  assert.ok(tuesday);
  assert.equal(tuesday.length, 1);
  assert.equal(tuesday[0]!.times[0], '04:30');
  assert.equal(tuesday[0]!.crossTimezone, true);
});

test('expandSchedulesForRange aggregates high-frequency schedules cleanly and detects DST shifts', () => {
  const hourly = makeSchedule({ id: 'hourly', name: '每小时巡检', cron_expression: '0 * * * *' });
  const everyMin = makeSchedule({ id: 'minutely', name: '每分钟心跳', cron_expression: '* * * * *' });
  // 2026-11-01 是 America/New_York 夏令时结束日（EDT -4 -> EST -5）
  const nyMorning = makeSchedule({
    id: 'ny',
    name: '纽约早报',
    cron_expression: '0 9 * * *',
    timezone: 'America/New_York',
  });

  const map = expandSchedulesForRange([hourly, everyMin, nyMorning], '2026-10-31', '2026-11-02', 'Asia/Shanghai');
  const oct31 = map.get('2026-10-31')!;
  const hourlyItem = oct31.find(x => x.schedule.id === 'hourly')!;
  assert.equal(hourlyItem.isHighFrequency, true);
  assert.equal(hourlyItem.totalTriggers, 24);
  assert.equal(hourlyItem.compactTimeLabel, '×24');

  const minutelyItem = oct31.find(x => x.schedule.id === 'minutely')!;
  assert.equal(minutelyItem.totalTriggers, 1440);
  assert.equal(minutelyItem.timesTruncated, true);
  assert.equal(minutelyItem.times.length, 48);

  // 2026-10-31 NY 09:00 EDT (-4) -> UTC 13:00 -> Shanghai 21:00
  const nyOct31 = oct31.find(x => x.schedule.id === 'ny')!;
  assert.equal(nyOct31.times[0], '21:00');
  // 2026-11-01 NY 09:00 EST (-5, 切换后) -> UTC 14:00 -> Shanghai 22:00
  const nyNov01 = map.get('2026-11-01')!.find(x => x.schedule.id === 'ny')!;
  assert.equal(nyNov01.times[0], '22:00');
});

test('buildWeekDayTimedEvents excludes high-frequency items and splits overlapping events into columns', () => {
  const a = makeSchedule({ id: 'a', name: 'A 晨报', cron_expression: '0 9 * * *' });
  const b = makeSchedule({ id: 'b', name: 'B 巡检', cron_expression: '15 9 * * *' });
  const c = makeSchedule({ id: 'c', name: 'C 同步', cron_expression: '30 9 * * *' });
  const d = makeSchedule({ id: 'd', name: 'D 晚报', cron_expression: '0 18 * * *' });
  const h = makeSchedule({ id: 'h', name: '高频', cron_expression: '0 * * * *' });

  const dayItems = expandSchedulesForRange([a, b, c, d, h], '2026-11-26', '2026-11-26', 'Asia/Shanghai').get('2026-11-26')!;
  // 30 分钟碰撞窗口下，09:00 与 09:30 复用列 0，仅需 2 列而非 3 列
  const events = buildWeekDayTimedEvents(dayItems, 30);
  assert.equal(events.length, 4);
  const evA = events.find(e => e.schedule.id === 'a')!;
  const evB = events.find(e => e.schedule.id === 'b')!;
  const evC = events.find(e => e.schedule.id === 'c')!;
  const evD = events.find(e => e.schedule.id === 'd')!;
  assert.equal(evA.columnCount, 2);
  assert.equal(evB.columnCount, 2);
  assert.equal(evC.columnCount, 2);
  assert.equal(evA.column, 0);
  assert.equal(evB.column, 1);
  assert.equal(evC.column, 0);
  assert.equal(evD.columnCount, 1);
  assert.equal(evD.column, 0);
});

test('previewNextTriggers computes upcoming occurrences skipping invalid calendar days', () => {
  const from = new Date('2026-11-26T00:00:00Z'); // Shanghai 08:00
  const previews = previewNextTriggers('0 9 * * 1-5', 'Asia/Shanghai', 'Asia/Shanghai', 3, from);
  assert.equal(previews.length, 3);
  assert.equal(previews[0]!.ymd, '2026-11-26'); // Thu 09:00
  assert.equal(previews[1]!.ymd, '2026-11-27'); // Fri 09:00
  assert.equal(previews[2]!.ymd, '2026-11-30'); // Mon 09:00 (skips weekend)

  // 每月 31 日从 2026-12-01 起应依次命中 12-31、01-31、03-31（跳过 2 月）
  const d31 = previewNextTriggers('0 9 31 * *', 'Asia/Shanghai', 'Asia/Shanghai', 3, new Date('2026-12-01T00:00:00Z'));
  assert.deepEqual(d31.map(x => x.ymd), ['2026-12-31', '2027-01-31', '2027-03-31']);
});
