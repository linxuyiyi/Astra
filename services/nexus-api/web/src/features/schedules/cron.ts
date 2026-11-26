/**
 * Cron 解析、可读描述、时区投影与日历展开引擎。
 *
 * 规则与 AgentScope 2.0.10 SchedulerManager (APScheduler CronTrigger) 保持一致：
 * - 标准 5 位表达式：分(0-59) 时(0-23) 日(1-31) 月(1-12/JAN-DEC) 周(0-7/SUN-SAT，0与7均为周日)
 * - 日与周同时受限时采用 AND 交集语义（与 APScheduler CronTrigger 一致）
 * - 先在日程自身 IANA timezone 下求值，再投影到日历展示时区
 */

import type { EmployeeSchedule } from '@/api';

export type FrequencyMode = 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'custom';

export const CRON_PRESETS = [
  { label: '每小时', cron: '0 * * * *' },
  { label: '每 2 小时', cron: '0 */2 * * *' },
  { label: '每天 09:00', cron: '0 9 * * *' },
  { label: '工作日 09:00', cron: '0 9 * * 1-5' },
  { label: '每周一 09:00', cron: '0 9 * * 1' },
  { label: '每月 1 日 09:00', cron: '0 9 1 * *' },
] as const;

export const FREQUENCIES: { value: FrequencyMode; label: string }[] = [
  { value: 'daily', label: '每天' },
  { value: 'weekdays', label: '工作日' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'custom', label: '自定义 Cron' },
];

export const WEEKDAYS = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'] as const;
export const SHORT_WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'] as const;
/** 周一至周日表头（中国与企业排班习惯） */
export const MONDAY_FIRST_WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'] as const;

export const COMMON_TIMEZONES = [
  'Asia/Shanghai',
  'Asia/Hong_Kong',
  'Asia/Tokyo',
  'UTC',
  'Europe/London',
  'America/New_York',
  'America/Los_Angeles',
] as const;

const DOW_ALIASES: Record<string, number> = {
  SUN: 0, MON: 1, TUE: 2, WED: 3, THU: 4, FRI: 5, SAT: 6,
};

const MONTH_ALIASES: Record<string, number> = {
  JAN: 1, FEB: 2, MAR: 3, APR: 4, MAY: 5, JUN: 6,
  JUL: 7, AUG: 8, SEP: 9, OCT: 10, NOV: 11, DEC: 12,
};

/** 超过该单日触发次数视为高频循环（周视图收纳到顶部高频条带，避免刷满 24h 时间轴） */
export const HIGH_FREQUENCY_THRESHOLD = 6;
/** 单日最多保留多少条明细时刻样本，防止每分钟任务占用过多内存 */
const MAX_STORED_TIMES_PER_DAY = 48;

export interface ParsedCron {
  raw: string;
  minutes: number[];
  hours: number[];
  daysOfMonth: Set<number>;
  months: Set<number>;
  daysOfWeek: Set<number>;
  dailyTriggerCount: number;
}

export interface CronDescription {
  label: string;
  valid: boolean;
  isInterval?: boolean;
}

export interface ZonedDateParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
  /** 0 = Sunday .. 6 = Saturday */
  dow: number;
}

export interface CalendarCell {
  ymd: string;
  year: number;
  month: number;
  day: number;
  /** 0 = Sunday .. 6 = Saturday */
  dow: number;
  isCurrentMonth: boolean;
  isToday: boolean;
  isWeekend: boolean;
}

export interface CalendarDaySchedule {
  schedule: EmployeeSchedule;
  ymd: string;
  /** 当天展示时区下的触发时刻（HH:mm，升序，高频时最多保留采样） */
  times: string[];
  timesTruncated: boolean;
  totalTriggers: number;
  isHighFrequency: boolean;
  /** 月视图单行紧凑前缀，例如 "09:00"、"09:00 ×3"、"×24" */
  compactTimeLabel: string;
  /** 侧栏/周视图高频条带摘要 */
  detailTimeLabel: string;
  ruleLabel: string;
  crossTimezone: boolean;
  toneIndex: number;
}

export interface WeekTimedEvent {
  key: string;
  schedule: EmployeeSchedule;
  ymd: string;
  time: string;
  startMinute: number;
  ruleLabel: string;
  crossTimezone: boolean;
  toneIndex: number;
  column: number;
  columnCount: number;
}

export interface TriggerPreviewItem {
  ymd: string;
  time: string;
  weekdayLabel: string;
  dateTimeLabel: string;
  scheduleTzLabel?: string;
}

export interface EmployeeToneSpec {
  accent: string;
  bg: string;
  bgHover: string;
  border: string;
  text: string;
}

/** 8 组克制冷调色阶，与 Codex 冷灰表面和谐共处 */
export const EMPLOYEE_TONES: readonly EmployeeToneSpec[] = [
  { accent: '#2563eb', bg: 'rgba(37, 99, 235, 0.08)', bgHover: 'rgba(37, 99, 235, 0.14)', border: 'rgba(37, 99, 235, 0.24)', text: '#1d4ed8' },
  { accent: '#059669', bg: 'rgba(5, 150, 105, 0.08)', bgHover: 'rgba(5, 150, 105, 0.14)', border: 'rgba(5, 150, 105, 0.24)', text: '#047857' },
  { accent: '#4f46e5', bg: 'rgba(79, 70, 229, 0.08)', bgHover: 'rgba(79, 70, 229, 0.14)', border: 'rgba(79, 70, 229, 0.24)', text: '#4338ca' },
  { accent: '#d97706', bg: 'rgba(217, 119, 6, 0.09)', bgHover: 'rgba(217, 119, 6, 0.15)', border: 'rgba(217, 119, 6, 0.26)', text: '#b45309' },
  { accent: '#0d9488', bg: 'rgba(13, 148, 136, 0.08)', bgHover: 'rgba(13, 148, 136, 0.14)', border: 'rgba(13, 148, 136, 0.24)', text: '#0f766e' },
  { accent: '#e11d48', bg: 'rgba(225, 29, 72, 0.075)', bgHover: 'rgba(225, 29, 72, 0.13)', border: 'rgba(225, 29, 72, 0.22)', text: '#be123c' },
  { accent: '#7c3aed', bg: 'rgba(124, 58, 237, 0.08)', bgHover: 'rgba(124, 58, 237, 0.14)', border: 'rgba(124, 58, 237, 0.24)', text: '#6d28d9' },
  { accent: '#475569', bg: 'rgba(71, 85, 105, 0.09)', bgHover: 'rgba(71, 85, 105, 0.15)', border: 'rgba(71, 85, 105, 0.24)', text: '#334155' },
];

export function employeeToneIndex(key: string | null | undefined): number {
  const source = (key || 'unknown').trim() || 'unknown';
  let hash = 2166136261;
  for (let i = 0; i < source.length; i++) {
    hash ^= source.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % EMPLOYEE_TONES.length;
}

export function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

export function formatYmd(year: number, month: number, day: number): string {
  return `${year}-${pad2(month)}-${pad2(day)}`;
}

export function parseYmd(ymd: string): { year: number; month: number; day: number } {
  const parts = ymd.split('-').map(Number);
  return {
    year: parts[0] || 2026,
    month: parts[1] || 1,
    day: parts[2] || 1,
  };
}

export function addDaysYmd(ymd: string, deltaDays: number): string {
  const { year, month, day } = parseYmd(ymd);
  const d = new Date(Date.UTC(year, month - 1, day + deltaDays));
  return formatYmd(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
}

export function dayOfWeekForYmd(ymd: string): number {
  const { year, month, day } = parseYmd(ymd);
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}

export function compareYmd(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

// ── 时区工具（原生 Intl.DateTimeFormat，带缓存） ───────────────────────────

const partsFormatterCache = new Map<string, Intl.DateTimeFormat | null>();

function getPartsFormatter(timeZone: string): Intl.DateTimeFormat | null {
  if (partsFormatterCache.has(timeZone)) return partsFormatterCache.get(timeZone)!;
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    });
    partsFormatterCache.set(timeZone, fmt);
    return fmt;
  } catch {
    partsFormatterCache.set(timeZone, null);
    return null;
  }
}

export function isValidTimezone(timeZone: string): boolean {
  return Boolean(timeZone && getPartsFormatter(timeZone));
}

export function getZonedParts(date: Date, timeZone: string): ZonedDateParts {
  const fmt = getPartsFormatter(timeZone) ?? getPartsFormatter('Asia/Shanghai')!;
  const parts = fmt.formatToParts(date);
  let year = 0;
  let month = 1;
  let day = 1;
  let hour = 0;
  let minute = 0;
  let second = 0;
  for (const part of parts) {
    if (part.type === 'year') year = Number(part.value);
    else if (part.type === 'month') month = Number(part.value);
    else if (part.type === 'day') day = Number(part.value);
    else if (part.type === 'hour') hour = Number(part.value) % 24;
    else if (part.type === 'minute') minute = Number(part.value);
    else if (part.type === 'second') second = Number(part.value);
  }
  const dow = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  return { year, month, day, hour, minute, second, dow };
}

export function todayYmdInTimezone(timeZone: string, now = new Date()): string {
  const p = getZonedParts(now, timeZone);
  return formatYmd(p.year, p.month, p.day);
}

/**
 * 将指定 IANA 时区的本地时间 (Y, M, D, h, m) 精确换算为 UTC Date。
 * 若处于夏令时跳过的非法空洞时刻则返回 null。
 */
export function zonedLocalToUtc(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute: number,
  timeZone: string,
): Date | null {
  if (!isValidTimezone(timeZone)) return null;
  const targetAsUtc = Date.UTC(year, month - 1, day, hour, minute, 0);

  const probe = (utcMs: number) => {
    const p = getZonedParts(new Date(utcMs), timeZone);
    const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, 0);
    return { p, asUtc, offset: asUtc - utcMs };
  };

  const first = probe(targetAsUtc);
  const candidate1 = targetAsUtc - first.offset;
  const second = probe(candidate1);
  if (second.asUtc === targetAsUtc) return new Date(candidate1);

  const candidate2 = targetAsUtc - second.offset;
  const third = probe(candidate2);
  if (third.asUtc === targetAsUtc) return new Date(candidate2);

  // 夏令时向前拨钟产生的不存在本地时刻
  return null;
}

/**
 * 获取某 UTC 时刻在指定时区相对 UTC 的偏移（分钟，如 Asia/Shanghai = +480）。
 */
function timezoneOffsetMinutes(utcMs: number, timeZone: string): number {
  const p = getZonedParts(new Date(utcMs), timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return Math.round((asUtc - utcMs) / 60000);
}

// ── Cron 解析与可读描述 ────────────────────────────────────────────────────

function parseCronAtom(
  token: string,
  minVal: number,
  maxVal: number,
  aliases?: Record<string, number>,
): number | null {
  const upper = token.toUpperCase();
  if (aliases && upper in aliases) return aliases[upper]!;
  if (!/^\d+$/.test(token)) return null;
  const num = Number(token);
  return num >= minVal && num <= maxVal ? num : null;
}

function parseCronFieldValues(
  field: string,
  minVal: number,
  maxVal: number,
  aliases?: Record<string, number>,
  normalizeValue?: (n: number) => number,
): number[] | null {
  if (!field) return null;
  const values = new Set<number>();
  const segments = field.split(',');
  for (const seg of segments) {
    if (!seg) return null;
    const [base, stepRaw, ...extra] = seg.split('/');
    if (extra.length) return null;
    let step = 1;
    if (stepRaw !== undefined) {
      if (!/^[1-9]\d*$/.test(stepRaw)) return null;
      step = Number(stepRaw);
    }

    let rangeStart: number;
    let rangeEnd: number;

    if (base === '*') {
      rangeStart = minVal;
      rangeEnd = maxVal;
    } else if (base!.includes('-')) {
      const [startRaw, endRaw, ...more] = base!.split('-');
      if (more.length || !startRaw || !endRaw) return null;
      const start = parseCronAtom(startRaw, minVal, maxVal, aliases);
      const end = parseCronAtom(endRaw, minVal, maxVal, aliases);
      if (start === null || end === null || start > end) return null;
      rangeStart = start;
      rangeEnd = end;
    } else {
      const single = parseCronAtom(base!, minVal, maxVal, aliases);
      if (single === null) return null;
      rangeStart = single;
      // 与 APScheduler 语义一致：a/step 表示从 a 到 maxVal 按步长递增；单值 a 表示仅 a
      rangeEnd = stepRaw !== undefined ? maxVal : single;
    }

    for (let v = rangeStart; v <= rangeEnd; v += step) {
      values.add(normalizeValue ? normalizeValue(v) : v);
    }
  }
  if (!values.size) return null;
  return [...values].sort((a, b) => a - b);
}

export function parseCronExpression(cron: string): ParsedCron | null {
  const trimmed = cron.trim();
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) return null;
  const [minField, hourField, domField, monthField, dowField] = parts as [
    string, string, string, string, string,
  ];

  const minutes = parseCronFieldValues(minField, 0, 59);
  const hours = parseCronFieldValues(hourField, 0, 23);
  const domList = parseCronFieldValues(domField, 1, 31);
  const monthList = parseCronFieldValues(monthField, 1, 12, MONTH_ALIASES);
  const dowList = parseCronFieldValues(dowField, 0, 7, DOW_ALIASES, n => n % 7);

  if (!minutes || !hours || !domList || !monthList || !dowList) return null;

  return {
    raw: trimmed,
    minutes,
    hours,
    daysOfMonth: new Set(domList),
    months: new Set(monthList),
    daysOfWeek: new Set(dowList),
    dailyTriggerCount: minutes.length * hours.length,
  };
}

function normalizeDowField(raw: string): string {
  return raw.toUpperCase().replace(/\b(SUN|MON|TUE|WED|THU|FRI|SAT)\b/g, m => String(DOW_ALIASES[m]!));
}

function formatDowLabel(dow: string): string | null {
  const norm = normalizeDowField(dow);
  if (norm === '*') return null;
  if (norm === '1-5' || norm === '1,2,3,4,5') return '工作日';
  if (norm === '0,6' || norm === '6,0' || norm === '6,7' || norm === '6-7') return '周末';
  if (/^[0-7]$/.test(norm)) return `每周${SHORT_WEEKDAYS[Number(norm) % 7]}`;
  const rangeMatch = /^([0-7])-([0-7])$/.exec(norm);
  if (rangeMatch) {
    const start = Number(rangeMatch[1]) % 7;
    const end = Number(rangeMatch[2]) % 7;
    return `每周${SHORT_WEEKDAYS[start]}至周${SHORT_WEEKDAYS[end]}`;
  }
  if (/^[0-7](,[0-7])+$/.test(norm)) {
    const days = [...new Set(norm.split(',').map(d => Number(d) % 7))];
    if (days.length <= 5) return `每周${days.map(d => SHORT_WEEKDAYS[d]).join('、')}`;
  }
  return null;
}

function formatTimePart(min: string, hour: string): { text: string; isInterval: boolean } | null {
  const isSingleMin = /^\d+$/.test(min);
  const isSingleHour = /^\d+$/.test(hour);
  const minStep = /^\*\/([1-9]\d*)$/.exec(min);
  const hourStep = /^\*\/([1-9]\d*)$/.exec(hour);
  const hourRange = /^(\d+)-(\d+)$/.exec(hour);
  const hourRangeStep = /^(\d+)-(\d+)\/([1-9]\d*)$/.exec(hour);

  if (min === '*' && hour === '*') return { text: '每分钟', isInterval: true };

  if (minStep && hour === '*') {
    const n = Number(minStep[1]);
    return { text: n === 1 ? '每分钟' : `每 ${n} 分钟`, isInterval: true };
  }

  if (minStep && hourRange) {
    const n = Number(minStep[1]);
    return {
      text: `${pad2(Number(hourRange[1]))}:00–${pad2(Number(hourRange[2]))}:59 每 ${n} 分钟`,
      isInterval: false,
    };
  }

  if (isSingleMin && hour === '*') {
    const m = Number(min);
    return { text: m === 0 ? '每小时整点' : `每小时第 ${m} 分`, isInterval: true };
  }

  if (isSingleMin && hourStep) {
    const m = Number(min);
    const n = Number(hourStep[1]);
    if (m === 0) return { text: n === 1 ? '每小时' : `每 ${n} 小时`, isInterval: true };
    return { text: `每 ${n} 小时（第 ${m} 分）`, isInterval: true };
  }

  if (isSingleMin && hourRange) {
    const m = Number(min);
    const suffix = pad2(m);
    return {
      text: `${pad2(Number(hourRange[1]))}:${suffix}–${pad2(Number(hourRange[2]))}:${suffix} 每小时`,
      isInterval: false,
    };
  }

  if (isSingleMin && hourRangeStep) {
    const m = Number(min);
    const suffix = pad2(m);
    return {
      text: `${pad2(Number(hourRangeStep[1]))}:${suffix}–${pad2(Number(hourRangeStep[2]))}:${suffix} 每 ${Number(hourRangeStep[3])} 小时`,
      isInterval: false,
    };
  }

  if (isSingleMin && isSingleHour) {
    return { text: `${pad2(Number(hour))}:${pad2(Number(min))}`, isInterval: false };
  }

  if (isSingleMin && /^\d+(,\d+)+$/.test(hour)) {
    const hours = hour.split(',').map(Number);
    if (hours.length <= 4) {
      const m = pad2(Number(min));
      return { text: hours.map(h => `${pad2(h)}:${m}`).join('、'), isInterval: false };
    }
  }

  if (/^\d+(,\d+)+$/.test(min) && hour === '*') {
    const mins = min.split(',').map(Number);
    if (mins.length === 2 && mins[0] === 0 && mins[1] === 30) {
      return { text: '每 30 分钟', isInterval: true };
    }
    if (mins.length <= 4) {
      return { text: `每小时第 ${mins.join('、')} 分`, isInterval: true };
    }
  }

  if (/^\d+(,\d+)+$/.test(min) && isSingleHour) {
    const mins = min.split(',').map(Number);
    if (mins.length <= 4) {
      const h = pad2(Number(hour));
      return { text: mins.map(m => `${h}:${pad2(m)}`).join('、'), isInterval: false };
    }
  }

  return null;
}

export function describeCron(cron: string): CronDescription {
  const parsed = parseCronExpression(cron);
  if (!parsed) return { label: '表达式格式无效', valid: false };

  const [min, hour, dom, month, dow] = parsed.raw.split(/\s+/) as [string, string, string, string, string];
  const timePart = formatTimePart(min, hour);
  if (!timePart) return { label: '自定义周期', valid: true };

  if (dom === '*' && month === '*') {
    if (dow === '*') {
      return {
        label: timePart.isInterval ? timePart.text : `每天 ${timePart.text}`,
        valid: true,
        isInterval: timePart.isInterval,
      };
    }
    const dowLabel = formatDowLabel(dow);
    if (dowLabel) {
      return {
        label: `${dowLabel} ${timePart.text}`,
        valid: true,
        isInterval: timePart.isInterval,
      };
    }
  }

  if (month === '*' && dow === '*') {
    if (/^\d+$/.test(dom)) {
      return { label: `每月 ${Number(dom)} 日 ${timePart.text}`, valid: true, isInterval: timePart.isInterval };
    }
    if (/^\d+(,\d+)+$/.test(dom)) {
      const days = dom.split(',').map(Number);
      if (days.length <= 4) {
        return { label: `每月 ${days.join('、')} 日 ${timePart.text}`, valid: true, isInterval: timePart.isInterval };
      }
    }
    const domStep = /^\*\/([1-9]\d*)$/.exec(dom);
    if (domStep) {
      return { label: `每 ${Number(domStep[1])} 天 ${timePart.text}`, valid: true, isInterval: timePart.isInterval };
    }
  }

  if (/^\d+$/.test(month) && /^\d+$/.test(dom) && dow === '*') {
    return { label: `每年 ${Number(month)} 月 ${Number(dom)} 日 ${timePart.text}`, valid: true };
  }

  const monthStep = /^\*\/([1-9]\d*)$/.exec(month);
  if (monthStep && /^\d+$/.test(dom) && dow === '*') {
    return { label: `每 ${Number(monthStep[1])} 个月 ${Number(dom)} 日 ${timePart.text}`, valid: true };
  }

  return { label: '自定义周期', valid: true };
}

export function restoreFrequencyFromCron(cron: string): {
  frequency: FrequencyMode;
  time: string;
  weekday: number;
  monthDay: number;
} {
  const parts = cron.trim().split(/\s+/);
  const [minute, hour, day, month, week] = parts;
  const fallback = { frequency: 'custom' as FrequencyMode, time: '09:00', weekday: 1, monthDay: 1 };
  const valid = parts.length === 5
    && /^\d+$/.test(minute!)
    && /^\d+$/.test(hour!)
    && Number(minute) <= 59
    && Number(hour) <= 23
    && month === '*';
  if (!valid) return fallback;
  const at = `${hour!.padStart(2, '0')}:${minute!.padStart(2, '0')}`;
  if (day === '*' && week === '*') return { ...fallback, frequency: 'daily', time: at };
  if (day === '*' && week === '1-5') return { ...fallback, frequency: 'weekdays', time: at };
  if (day === '*' && /^[0-7]$/.test(week!)) {
    return { ...fallback, frequency: 'weekly', weekday: Number(week) % 7, time: at };
  }
  if (week === '*' && /^\d+$/.test(day!) && Number(day) >= 1 && Number(day) <= 31) {
    return { ...fallback, frequency: 'monthly', monthDay: Number(day), time: at };
  }
  return fallback;
}

// ── 月历 / 周历网格构建 ────────────────────────────────────────────────────

/**
 * 构建月历网格（周一为每周第一天，自动 5 周或 6 周，恰好覆盖整月）。
 */
export function buildMonthGrid(year: number, month: number, todayYmd: string): CalendarCell[] {
  const firstDow = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const leadingDays = (firstDow + 6) % 7; // 周一 = 0, 周日 = 6
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const totalCells = Math.ceil((leadingDays + daysInMonth) / 7) * 7;

  const startYmd = addDaysYmd(formatYmd(year, month, 1), -leadingDays);
  const cells: CalendarCell[] = [];
  for (let i = 0; i < totalCells; i++) {
    const ymd = addDaysYmd(startYmd, i);
    const { year: y, month: m, day: d } = parseYmd(ymd);
    const dow = dayOfWeekForYmd(ymd);
    cells.push({
      ymd,
      year: y,
      month: m,
      day: d,
      dow,
      isCurrentMonth: y === year && m === month,
      isToday: ymd === todayYmd,
      isWeekend: dow === 0 || dow === 6,
    });
  }
  return cells;
}

/**
 * 构建包含 anchorYmd 的一周 7 天（周一到周日）。
 */
export function buildWeekDays(anchorYmd: string, todayYmd: string): CalendarCell[] {
  const anchorDow = dayOfWeekForYmd(anchorYmd);
  const offsetToMonday = (anchorDow + 6) % 7;
  const mondayYmd = addDaysYmd(anchorYmd, -offsetToMonday);
  const days: CalendarCell[] = [];
  for (let i = 0; i < 7; i++) {
    const ymd = addDaysYmd(mondayYmd, i);
    const { year, month, day } = parseYmd(ymd);
    const dow = dayOfWeekForYmd(ymd);
    days.push({
      ymd,
      year,
      month,
      day,
      dow,
      isCurrentMonth: true,
      isToday: ymd === todayYmd,
      isWeekend: dow === 0 || dow === 6,
    });
  }
  return days;
}

// ── 日历范围展开核心 ───────────────────────────────────────────────────────

interface MutableDayBucket {
  schedule: EmployeeSchedule;
  times: string[];
  totalTriggers: number;
  timesTruncated: boolean;
  ruleLabel: string;
  crossTimezone: boolean;
  toneIndex: number;
}

function buildCompactTimeLabel(bucket: MutableDayBucket): string {
  if (bucket.totalTriggers === 1) return bucket.times[0] || '—';
  if (bucket.totalTriggers < HIGH_FREQUENCY_THRESHOLD) {
    return `${bucket.times[0]} 等${bucket.totalTriggers}次`;
  }
  return `×${bucket.totalTriggers}`;
}

function buildDetailTimeLabel(bucket: MutableDayBucket): string {
  if (bucket.totalTriggers === 1) return bucket.times[0] || '—';
  if (bucket.totalTriggers <= 4) return bucket.times.join('、');
  return `${bucket.ruleLabel} · 当日 ${bucket.totalTriggers} 次`;
}

function appendOccurrence(
  dayBuckets: Map<string, Map<string, MutableDayBucket>>,
  displayYmd: string,
  displayTime: string,
  schedule: EmployeeSchedule,
  ruleLabel: string,
  crossTimezone: boolean,
  toneIndex: number,
  rangeStartYmd: string,
  rangeEndYmd: string,
) {
  if (compareYmd(displayYmd, rangeStartYmd) < 0 || compareYmd(displayYmd, rangeEndYmd) > 0) return;
  let perSchedule = dayBuckets.get(displayYmd);
  if (!perSchedule) {
    perSchedule = new Map();
    dayBuckets.set(displayYmd, perSchedule);
  }
  let bucket = perSchedule.get(schedule.id);
  if (!bucket) {
    bucket = {
      schedule,
      times: [],
      totalTriggers: 0,
      timesTruncated: false,
      ruleLabel,
      crossTimezone,
      toneIndex,
    };
    perSchedule.set(schedule.id, bucket);
  }
  bucket.totalTriggers += 1;
  if (bucket.times.length < MAX_STORED_TIMES_PER_DAY) {
    bucket.times.push(displayTime);
  } else {
    bucket.timesTruncated = true;
  }
}

/**
 * 将一组日程按 Cron 表达式展开到 [rangeStartYmd, rangeEndYmd]（展示时区）的每一天。
 */
export function expandSchedulesForRange(
  schedules: EmployeeSchedule[],
  rangeStartYmd: string,
  rangeEndYmd: string,
  displayTimezone: string,
): Map<string, CalendarDaySchedule[]> {
  const safeDisplayTz = isValidTimezone(displayTimezone) ? displayTimezone : 'Asia/Shanghai';
  // 扫描时在起止边界外各扩 1 天，覆盖跨时区偏移导致的跨日投影
  const scanStart = addDaysYmd(rangeStartYmd, -1);
  const scanEnd = addDaysYmd(rangeEndYmd, 1);

  const scanDates: { ymd: string; year: number; month: number; day: number; dow: number }[] = [];
  for (let cur = scanStart; compareYmd(cur, scanEnd) <= 0; cur = addDaysYmd(cur, 1)) {
    const { year, month, day } = parseYmd(cur);
    scanDates.push({ ymd: cur, year, month, day, dow: dayOfWeekForYmd(cur) });
  }

  const dayBuckets = new Map<string, Map<string, MutableDayBucket>>();

  for (const schedule of schedules) {
    const parsed = parseCronExpression(schedule.data.cron_expression);
    if (!parsed) continue;
    const scheduleTz = isValidTimezone(schedule.data.timezone) ? schedule.data.timezone : safeDisplayTz;
    const crossTimezone = scheduleTz !== safeDisplayTz;
    const ruleLabel = describeCron(schedule.data.cron_expression).label;
    const toneIndex = employeeToneIndex(schedule.employee_id || schedule.employee_name);

    for (const d of scanDates) {
      if (
        !parsed.months.has(d.month)
        || !parsed.daysOfMonth.has(d.day)
        || !parsed.daysOfWeek.has(d.dow)
      ) {
        continue;
      }

      if (!crossTimezone) {
        // 同日历时区快速路径：无需逐分钟调用 Intl
        if (compareYmd(d.ymd, rangeStartYmd) < 0 || compareYmd(d.ymd, rangeEndYmd) > 0) continue;
        let perSchedule = dayBuckets.get(d.ymd);
        if (!perSchedule) {
          perSchedule = new Map();
          dayBuckets.set(d.ymd, perSchedule);
        }
        const times: string[] = [];
        let truncated = false;
        outer: for (const h of parsed.hours) {
          for (const m of parsed.minutes) {
            if (times.length >= MAX_STORED_TIMES_PER_DAY) {
              truncated = true;
              break outer;
            }
            times.push(`${pad2(h)}:${pad2(m)}`);
          }
        }
        perSchedule.set(schedule.id, {
          schedule,
          times,
          totalTriggers: parsed.dailyTriggerCount,
          timesTruncated: truncated,
          ruleLabel,
          crossTimezone: false,
          toneIndex,
        });
        continue;
      }

      // 跨时区路径：检测当天是否跨越任一时区的夏令时切换点
      const startUtc = zonedLocalToUtc(d.year, d.month, d.day, 0, 0, scheduleTz);
      const endUtc = zonedLocalToUtc(d.year, d.month, d.day, 23, 59, scheduleTz);
      const constantDelta = startUtc && endUtc
        ? (() => {
            const deltaStart = timezoneOffsetMinutes(startUtc.getTime(), safeDisplayTz)
              - timezoneOffsetMinutes(startUtc.getTime(), scheduleTz);
            const deltaEnd = timezoneOffsetMinutes(endUtc.getTime(), safeDisplayTz)
              - timezoneOffsetMinutes(endUtc.getTime(), scheduleTz);
            return deltaStart === deltaEnd ? deltaStart : null;
          })()
        : null;

      if (constantDelta !== null) {
        for (const h of parsed.hours) {
          for (const m of parsed.minutes) {
            const totalMin = h * 60 + m + constantDelta;
            const dayShift = Math.floor(totalMin / 1440);
            const wrappedMin = ((totalMin % 1440) + 1440) % 1440;
            const displayYmd = dayShift === 0 ? d.ymd : addDaysYmd(d.ymd, dayShift);
            const displayTime = `${pad2(Math.floor(wrappedMin / 60))}:${pad2(wrappedMin % 60)}`;
            appendOccurrence(
              dayBuckets,
              displayYmd,
              displayTime,
              schedule,
              ruleLabel,
              true,
              toneIndex,
              rangeStartYmd,
              rangeEndYmd,
            );
          }
        }
      } else {
        // 夏令时切换日：逐个触发点精确求值
        for (const h of parsed.hours) {
          for (const m of parsed.minutes) {
            const utc = zonedLocalToUtc(d.year, d.month, d.day, h, m, scheduleTz);
            if (!utc) continue;
            const dp = getZonedParts(utc, safeDisplayTz);
            const displayYmd = formatYmd(dp.year, dp.month, dp.day);
            const displayTime = `${pad2(dp.hour)}:${pad2(dp.minute)}`;
            appendOccurrence(
              dayBuckets,
              displayYmd,
              displayTime,
              schedule,
              ruleLabel,
              true,
              toneIndex,
              rangeStartYmd,
              rangeEndYmd,
            );
          }
        }
      }
    }
  }

  const result = new Map<string, CalendarDaySchedule[]>();
  for (const [ymd, perSchedule] of dayBuckets) {
    const items: CalendarDaySchedule[] = [];
    for (const bucket of perSchedule.values()) {
      bucket.times.sort();
      const isHighFrequency = bucket.totalTriggers >= HIGH_FREQUENCY_THRESHOLD;
      items.push({
        schedule: bucket.schedule,
        ymd,
        times: bucket.times,
        timesTruncated: bucket.timesTruncated,
        totalTriggers: bucket.totalTriggers,
        isHighFrequency,
        compactTimeLabel: buildCompactTimeLabel(bucket),
        detailTimeLabel: buildDetailTimeLabel(bucket),
        ruleLabel: bucket.ruleLabel,
        crossTimezone: bucket.crossTimezone,
        toneIndex: bucket.toneIndex,
      });
    }
    // 排序：启用的优先于已暂停；按首次触发时间升序；同时间按名称排序
    items.sort((a, b) => {
      if (a.schedule.data.enabled !== b.schedule.data.enabled) {
        return a.schedule.data.enabled ? -1 : 1;
      }
      const ta = a.times[0] || '99:99';
      const tb = b.times[0] || '99:99';
      if (ta !== tb) return ta.localeCompare(tb);
      return a.schedule.data.name.localeCompare(b.schedule.data.name, 'zh-CN');
    });
    result.set(ymd, items);
  }
  return result;
}

/**
 * 为周视图某一天生成纵向时间轴事件列表，并计算重叠分栏坐标。
 * 视觉事件块默认按 45 分钟高度估算碰撞窗口，确保同一时刻并排展示互不遮挡。
 */
export function buildWeekDayTimedEvents(
  dayItems: CalendarDaySchedule[],
  overlapWindowMinutes = 45,
): WeekTimedEvent[] {
  const raw: Omit<WeekTimedEvent, 'column' | 'columnCount'>[] = [];
  for (const item of dayItems) {
    if (item.isHighFrequency) continue;
    for (const time of item.times) {
      const [h, m] = time.split(':').map(Number);
      const startMinute = (h || 0) * 60 + (m || 0);
      raw.push({
        key: `${item.schedule.id}::${item.ymd}::${time}`,
        schedule: item.schedule,
        ymd: item.ymd,
        time,
        startMinute,
        ruleLabel: item.ruleLabel,
        crossTimezone: item.crossTimezone,
        toneIndex: item.toneIndex,
      });
    }
  }
  raw.sort((a, b) => {
    if (a.startMinute !== b.startMinute) return a.startMinute - b.startMinute;
    if (a.schedule.data.enabled !== b.schedule.data.enabled) {
      return a.schedule.data.enabled ? -1 : 1;
    }
    return a.schedule.data.name.localeCompare(b.schedule.data.name, 'zh-CN');
  });

  // 按重叠连通分量做贪心列分配
  const positioned: WeekTimedEvent[] = [];
  let i = 0;
  while (i < raw.length) {
    const cluster: Omit<WeekTimedEvent, 'column' | 'columnCount'>[] = [raw[i]!];
    let clusterEnd = raw[i]!.startMinute + overlapWindowMinutes;
    let j = i + 1;
    while (j < raw.length && raw[j]!.startMinute < clusterEnd) {
      cluster.push(raw[j]!);
      clusterEnd = Math.max(clusterEnd, raw[j]!.startMinute + overlapWindowMinutes);
      j++;
    }

    const colEnds: number[] = [];
    const assigned: { event: Omit<WeekTimedEvent, 'column' | 'columnCount'>; col: number }[] = [];
    for (const ev of cluster) {
      let col = colEnds.findIndex(end => end <= ev.startMinute);
      if (col === -1) {
        col = colEnds.length;
        colEnds.push(ev.startMinute + overlapWindowMinutes);
      } else {
        colEnds[col] = ev.startMinute + overlapWindowMinutes;
      }
      assigned.push({ event: ev, col });
    }
    const columnCount = Math.max(1, colEnds.length);
    for (const { event, col } of assigned) {
      positioned.push({ ...event, column: col, columnCount });
    }
    i = j;
  }
  return positioned;
}

/**
 * 推演接下来 N 次计划触发（供新建/编辑抽屉的表达式校验示例展示）。
 */
export function previewNextTriggers(
  cron: string,
  scheduleTimezone: string,
  displayTimezone: string,
  count = 4,
  fromDate = new Date(),
): TriggerPreviewItem[] {
  const parsed = parseCronExpression(cron);
  if (!parsed || count <= 0) return [];
  const safeScheduleTz = isValidTimezone(scheduleTimezone) ? scheduleTimezone : 'Asia/Shanghai';
  const safeDisplayTz = isValidTimezone(displayTimezone) ? displayTimezone : safeScheduleTz;

  const startParts = getZonedParts(fromDate, safeScheduleTz);
  const startYmd = formatYmd(startParts.year, startParts.month, startParts.day);
  const fromMs = fromDate.getTime();
  const items: TriggerPreviewItem[] = [];

  // 最多扫描 400 天（足以覆盖每年 1 次及闰年外的跨年规则）
  for (let offset = 0; offset <= 400 && items.length < count; offset++) {
    const ymd = offset === 0 ? startYmd : addDaysYmd(startYmd, offset);
    const { year, month, day } = parseYmd(ymd);
    const dow = dayOfWeekForYmd(ymd);
    if (
      !parsed.months.has(month)
      || !parsed.daysOfMonth.has(day)
      || !parsed.daysOfWeek.has(dow)
    ) {
      continue;
    }

    for (const h of parsed.hours) {
      if (offset === 0 && h < startParts.hour) continue;
      for (const m of parsed.minutes) {
        if (offset === 0 && h === startParts.hour && m <= startParts.minute) continue;
        const utc = zonedLocalToUtc(year, month, day, h, m, safeScheduleTz);
        if (!utc || utc.getTime() <= fromMs) continue;

        const dp = getZonedParts(utc, safeDisplayTz);
        const displayYmd = formatYmd(dp.year, dp.month, dp.day);
        const displayTime = `${pad2(dp.hour)}:${pad2(dp.minute)}`;
        const weekdayLabel = `周${SHORT_WEEKDAYS[dp.dow]}`;
        items.push({
          ymd: displayYmd,
          time: displayTime,
          weekdayLabel,
          dateTimeLabel: `${pad2(dp.month)}-${pad2(dp.day)} (${weekdayLabel}) ${displayTime}`,
          scheduleTzLabel: safeScheduleTz !== safeDisplayTz
            ? `${pad2(month)}-${pad2(day)} ${pad2(h)}:${pad2(m)} ${safeScheduleTz}`
            : undefined,
        });
        if (items.length >= count) break;
      }
      if (items.length >= count) break;
    }
  }
  return items;
}
