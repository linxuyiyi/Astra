<template>
  <div class="week-cal" data-testid="schedules-calendar-week">
    <!-- 单一滚动视口包裹表头、高频条带与 24h 时间轴：
         1) 纵向滚条作用于同一画布，消除 6~7px 列边界错位
         2) 窄屏下三者同步横向滚动，彻底避免表头与卡片挤压重叠 -->
    <div ref="scrollRef" class="week-cal__scroll">
      <div class="week-cal__canvas">
        <div class="week-cal__sticky-top">
          <!-- 周表头 -->
          <div class="week-cal__header">
            <div class="week-cal__gutter-head" aria-hidden="true" />
            <div class="week-cal__day-heads">
              <div
                v-for="(day, idx) in days"
                :key="day.ymd"
                class="week-day-head"
                :class="{
                  'is-today': day.isToday,
                  'is-weekend': day.isWeekend,
                  'is-selected': day.ymd === selectedYmd,
                }"
                @click="emit('select-date', day.ymd)"
              >
                <div class="week-day-head__top">
                  <span class="week-day-head__dow">{{ MONDAY_FIRST_WEEKDAYS[idx] }}</span>
                  <button
                    type="button"
                    class="week-day-head__add"
                    :aria-label="`在 ${day.ymd} 新建日程`"
                    title="在此日新建日程"
                    @click.stop="emit('create-at', { ymd: day.ymd })"
                  >
                    <Plus :size="12" aria-hidden="true" />
                  </button>
                </div>
                <div class="week-day-head__bottom">
                  <span class="week-day-head__num" :class="{ 'is-today-badge': day.isToday }">
                    {{ day.day }}
                  </span>
                  <span v-if="dayTotalCount(day.ymd) > 0" class="week-day-head__count">
                    {{ dayTotalCount(day.ymd) }} 项
                  </span>
                </div>
              </div>
            </div>
          </div>

          <!-- 高频循环条带（当日触发 >= 6 次的巡检日程集中展示，避免淹没 24 小时时间轴） -->
          <div v-if="hasAnyHighFrequency" class="week-cal__strip">
            <div class="week-cal__strip-gutter" title="每日触发 6 次及以上的高频循环日程">
              <Repeat :size="12" aria-hidden="true" />
              <span>高频</span>
            </div>
            <div class="week-cal__strip-cols">
              <div
                v-for="day in days"
                :key="`strip-${day.ymd}`"
                class="week-strip-col"
                :class="{ 'is-selected': day.ymd === selectedYmd, 'is-weekend': day.isWeekend }"
                @click="emit('select-date', day.ymd)"
              >
                <button
                  v-for="item in highFrequencyItems(day.ymd)"
                  :key="`hf-${day.ymd}-${item.schedule.id}`"
                  type="button"
                  class="strip-pill"
                  :class="{
                    'is-paused': !item.schedule.data.enabled,
                    'is-linked': hoveredScheduleId === item.schedule.id,
                  }"
                  :style="toneStyle(item.toneIndex, item.schedule.data.enabled)"
                  :title="`${item.schedule.data.name} · ${item.schedule.employee_name} · ${item.detailTimeLabel}`"
                  @mouseenter="emit('hover-schedule', item.schedule.id)"
                  @mouseleave="emit('hover-schedule', '')"
                  @click.stop="handleOpen(day.ymd, item.schedule)"
                >
                  <span class="strip-pill__freq">{{ item.compactTimeLabel }}</span>
                  <span class="strip-pill__name">{{ item.schedule.data.name }}</span>
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- 24 小时纵向时间轴 -->
        <div class="week-cal__body" :style="{ height: `${24 * HOUR_HEIGHT}px` }">
          <!-- 左侧整点时间刻度 -->
          <div class="week-cal__axis" aria-hidden="true">
            <div
              v-for="hour in HOURS"
              :key="hour"
              class="week-axis-label"
              :style="{ top: `${hour * HOUR_HEIGHT}px` }"
            >
              <span v-if="hour > 0">{{ pad2(hour) }}:00</span>
            </div>
          </div>

          <!-- 7 天列 -->
          <div class="week-cal__cols">
            <div
              v-for="(day, dayIdx) in days"
              :key="`col-${day.ymd}`"
              class="week-col"
              :class="{ 'is-selected': day.ymd === selectedYmd, 'is-weekend': day.isWeekend }"
            >
              <!-- 小时格（点击空白小时格可带入该日期与整点快速新建） -->
              <div
                v-for="hour in HOURS"
                :key="`slot-${day.ymd}-${hour}`"
                class="week-slot"
                :style="{ height: `${HOUR_HEIGHT}px` }"
                :title="`点击在 ${day.ymd} ${pad2(hour)}:00 新建日程`"
                @click="emit('create-at', { ymd: day.ymd, hour })"
              >
                <span class="week-slot__hint">+ {{ pad2(hour) }}:00</span>
              </div>

              <!-- 当前时间指示线 -->
              <div
                v-if="day.isToday"
                class="week-now-line"
                :style="{ top: `${nowTopPx}px` }"
                aria-hidden="true"
              >
                <i class="week-now-line__dot" />
              </div>

              <!-- 精确时间事件块 -->
              <button
                v-for="ev in timedEventsByDay.get(day.ymd) ?? []"
                :key="ev.key"
                type="button"
                class="week-event"
                :class="{
                  'is-paused': !ev.schedule.data.enabled,
                  'is-linked': hoveredScheduleId === ev.schedule.id,
                  'is-compact': ev.columnCount >= 2,
                  'is-right-edge': dayIdx >= 5 && ev.columnCount >= 2,
                }"
                :style="eventStyle(ev)"
                :title="eventTooltip(ev)"
                @mouseenter="emit('hover-schedule', ev.schedule.id)"
                @mouseleave="emit('hover-schedule', '')"
                @click.stop="handleOpen(day.ymd, ev.schedule)"
              >
                <div class="week-event__line">
                  <span class="week-event__time">{{ ev.time }}</span>
                  <span class="week-event__title">{{ ev.schedule.data.name }}</span>
                </div>
                <div v-if="ev.columnCount === 1" class="week-event__emp">{{ ev.schedule.employee_name }}</div>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Plus, Repeat } from 'lucide-vue-next';
import type { EmployeeSchedule } from '@/api';
import {
  buildWeekDayTimedEvents,
  EMPLOYEE_TONES,
  getZonedParts,
  MONDAY_FIRST_WEEKDAYS,
  pad2,
  type CalendarCell,
  type CalendarDaySchedule,
  type WeekTimedEvent,
} from '@/features/schedules/cron';

/** 每小时 72px、单事件卡 34px、碰撞窗口 30 分钟：
 *  09:00 与 09:30 自动复用同一列，避免 15 分钟间隔的三条日程被挤成 3 窄列。 */
const HOUR_HEIGHT = 72;
const EVENT_HEIGHT = 34;
const COMPACT_EVENT_HEIGHT = 28;
const OVERLAP_WINDOW_MINUTES = 30;
const HOURS = Array.from({ length: 24 }, (_, idx) => idx);

const props = defineProps<{
  days: CalendarCell[];
  dayMap: Map<string, CalendarDaySchedule[]>;
  selectedYmd: string;
  hoveredScheduleId: string;
}>();

const emit = defineEmits<{
  'select-date': [ymd: string];
  'open-schedule': [schedule: EmployeeSchedule];
  'create-at': [payload: { ymd: string; hour?: number }];
  'hover-schedule': [scheduleId: string];
}>();

const scrollRef = ref<HTMLElement | null>(null);
const nowTick = ref(Date.now());
let timer = 0;

const nowTopPx = computed(() => {
  const parts = getZonedParts(new Date(nowTick.value), 'Asia/Shanghai');
  return ((parts.hour * 60 + parts.minute) / 60) * HOUR_HEIGHT;
});

function dayTotalCount(ymd: string): number {
  return (props.dayMap.get(ymd) ?? []).length;
}

function highFrequencyItems(ymd: string): CalendarDaySchedule[] {
  return (props.dayMap.get(ymd) ?? []).filter(item => item.isHighFrequency);
}

const hasAnyHighFrequency = computed(() =>
  props.days.some(day => highFrequencyItems(day.ymd).length > 0),
);

const timedEventsByDay = computed(() => {
  const map = new Map<string, WeekTimedEvent[]>();
  for (const day of props.days) {
    const items = props.dayMap.get(day.ymd) ?? [];
    map.set(day.ymd, buildWeekDayTimedEvents(items, OVERLAP_WINDOW_MINUTES));
  }
  return map;
});

function toneStyle(toneIndex: number, enabled: boolean): Record<string, string> {
  if (!enabled) return {};
  const tone = EMPLOYEE_TONES[toneIndex] ?? EMPLOYEE_TONES[0]!;
  return {
    '--ev-accent': tone.accent,
    '--ev-bg': tone.bg,
    '--ev-bg-hover': tone.bgHover,
    '--ev-border': tone.border,
    '--ev-text': tone.text,
  };
}

function eventStyle(ev: WeekTimedEvent): Record<string, string> {
  const height = ev.columnCount >= 2 ? COMPACT_EVENT_HEIGHT : EVENT_HEIGHT;
  const rawTop = (ev.startMinute / 60) * HOUR_HEIGHT;
  const maxTop = 24 * HOUR_HEIGHT - height - 2;
  const top = Math.min(Math.max(2, rawTop), maxTop);
  const widthPct = 100 / ev.columnCount;
  const leftPct = ev.column * widthPct;
  return {
    ...toneStyle(ev.toneIndex, ev.schedule.data.enabled),
    top: `${top}px`,
    height: `${height}px`,
    left: `calc(${leftPct}% + 2px)`,
    width: `calc(${widthPct}% - 4px)`,
  };
}

function eventTooltip(ev: WeekTimedEvent): string {
  const status = ev.schedule.data.enabled ? '已启用' : '已暂停';
  return `${ev.time} · ${ev.schedule.data.name}\n员工：${ev.schedule.employee_name}\n规则：${ev.ruleLabel} · ${status}`;
}

function handleOpen(ymd: string, schedule: EmployeeSchedule) {
  emit('select-date', ymd);
  emit('open-schedule', schedule);
}

function scrollToHour(hour: number) {
  const el = scrollRef.value;
  if (!el) return;
  const targetHour = Math.max(0, Math.min(20, hour - 1));
  el.scrollTop = targetHour * HOUR_HEIGHT;
}

function scrollToRelevantHour() {
  const el = scrollRef.value;
  if (!el) return;
  let earliestMinute = 8 * 60;
  for (const list of timedEventsByDay.value.values()) {
    for (const ev of list) {
      if (ev.startMinute < earliestMinute) earliestMinute = ev.startMinute;
    }
  }
  scrollToHour(Math.floor(earliestMinute / 60));
}

function captureScrollTop(): number {
  return scrollRef.value?.scrollTop ?? 0;
}

function restoreScrollTop(top: number) {
  if (scrollRef.value) scrollRef.value.scrollTop = top;
}

defineExpose({ scrollToHour, captureScrollTop, restoreScrollTop });

onMounted(() => {
  void nextTick(scrollToRelevantHour);
  timer = window.setInterval(() => {
    nowTick.value = Date.now();
  }, 60_000);
});

watch(
  () => props.days[0]?.ymd,
  () => {
    void nextTick(scrollToRelevantHour);
  },
);

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<style scoped>
.week-cal {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--astra-bg-panel);
}

.week-cal__scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  position: relative;
}

.week-cal__canvas {
  min-width: 680px;
  display: flex;
  flex-direction: column;
}

.week-cal__sticky-top {
  position: sticky;
  top: 0;
  z-index: 8;
  background: var(--astra-bg-panel);
}

.week-cal__header {
  display: flex;
  flex: none;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}

.week-cal__gutter-head {
  flex: none;
  width: 58px;
  border-right: 1px solid var(--astra-border);
}

.week-cal__day-heads {
  flex: 1 1 auto;
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

.week-day-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  overflow: hidden;
  padding: 6px 8px 7px;
  border-right: 1px solid var(--astra-border-subtle);
  cursor: pointer;
  transition: background-color 0.14s ease;
}

.week-day-head:last-child {
  border-right: 0;
}

.week-day-head:hover {
  background: var(--astra-bg-base);
}

.week-day-head.is-selected {
  background: color-mix(in srgb, var(--astra-accent-soft) 55%, var(--astra-bg-panel));
  box-shadow: inset 0 -2px 0 var(--astra-primary);
}

.week-day-head__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  min-width: 0;
}

.week-day-head__dow {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
}

.week-day-head__add {
  display: inline-grid;
  place-items: center;
  flex: none;
  width: 18px;
  height: 18px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--astra-text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease, background-color 0.12s ease, color 0.12s ease;
}

.week-day-head:hover .week-day-head__add,
.week-day-head.is-selected .week-day-head__add,
.week-day-head__add:focus-visible {
  opacity: 1;
}

.week-day-head__add:hover {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
}

.week-day-head__bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  min-width: 0;
}

.week-day-head__num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  min-width: 24px;
  height: 24px;
  padding: 0 5px;
  border-radius: 999px;
  color: var(--astra-text-primary);
  font-size: 13.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.week-day-head__num.is-today-badge {
  background: var(--astra-primary);
  color: #ffffff;
}

.week-day-head__count {
  color: var(--astra-text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 高频循环区 ─────────────────────────────────────────────── */
.week-cal__strip {
  display: flex;
  flex: none;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-panel);
  max-height: 92px;
  overflow-y: auto;
}

.week-cal__strip-gutter {
  flex: none;
  width: 58px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 6px 4px;
  border-right: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 10.5px;
  font-weight: 600;
}

.week-cal__strip-cols {
  flex: 1 1 auto;
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

.week-strip-col {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 4px 5px;
  border-right: 1px solid var(--astra-border-subtle);
  min-width: 0;
}

.week-strip-col:last-child {
  border-right: 0;
}

.week-strip-col.is-weekend {
  background: color-mix(in srgb, var(--astra-bg-subtle) 55%, var(--astra-bg-panel));
}

.week-strip-col.is-selected {
  background: color-mix(in srgb, var(--astra-accent-soft) 35%, var(--astra-bg-panel));
}

.strip-pill {
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  min-width: 0;
  height: 20px;
  padding: 0 5px;
  border: 1px solid var(--ev-border, var(--astra-border));
  border-left: 3px solid var(--ev-accent, var(--astra-primary));
  border-radius: 4px;
  background: var(--ev-bg, var(--astra-bg-subtle));
  color: var(--astra-text-primary);
  font-size: 11px;
  line-height: 1;
  text-align: left;
  cursor: pointer;
}

.strip-pill:hover,
.strip-pill.is-linked {
  background: var(--ev-bg-hover, var(--astra-bg-elevated));
  box-shadow: 0 0 0 1.5px var(--ev-accent, var(--astra-primary));
}

.strip-pill.is-paused {
  border: 1px dashed var(--astra-border-hover);
  border-left: 3px dashed var(--astra-neutral);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
}

.strip-pill__freq {
  flex: none;
  color: var(--ev-text, var(--astra-text-secondary));
  font-family: var(--astra-font-mono);
  font-size: 10px;
  font-weight: 600;
}

.strip-pill.is-paused .strip-pill__freq {
  color: var(--astra-text-muted);
}

.strip-pill__name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}

/* ── 24 小时时间轴主体 ──────────────────────────────────────── */
.week-cal__body {
  display: flex;
  position: relative;
  flex: none;
}

.week-cal__axis {
  flex: none;
  width: 58px;
  position: relative;
  border-right: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}

.week-axis-label {
  position: absolute;
  left: 0;
  right: 0;
  transform: translateY(-50%);
  padding-right: 8px;
  text-align: right;
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.week-cal__cols {
  flex: 1 1 auto;
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

.week-col {
  position: relative;
  border-right: 1px solid var(--astra-border-subtle);
  min-width: 0;
}

.week-col:last-child {
  border-right: 0;
}

.week-col.is-weekend {
  background: color-mix(in srgb, var(--astra-bg-subtle) 55%, var(--astra-bg-panel));
}

.week-col.is-selected {
  background: color-mix(in srgb, var(--astra-accent-soft) 24%, var(--astra-bg-panel));
}

.week-slot {
  position: relative;
  border-bottom: 1px solid var(--astra-border-subtle);
  cursor: pointer;
}

.week-slot::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  border-top: 1px dashed color-mix(in srgb, var(--astra-border-subtle) 70%, transparent);
  pointer-events: none;
}

.week-slot:hover {
  background: color-mix(in srgb, var(--astra-primary-soft) 70%, transparent);
}

.week-slot__hint {
  position: absolute;
  top: 4px;
  right: 5px;
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 10px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.12s ease;
}

.week-slot:hover .week-slot__hint {
  opacity: 0.9;
}

.week-now-line {
  position: absolute;
  left: 0;
  right: 0;
  height: 1.5px;
  background: var(--astra-danger);
  z-index: 4;
  pointer-events: none;
}

.week-now-line__dot {
  position: absolute;
  left: -4px;
  top: -3.5px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--astra-danger);
}

.week-event {
  position: absolute;
  z-index: 2;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 1px;
  padding: 3px 5px;
  border: 1px solid var(--ev-border, var(--astra-border));
  border-left: 3px solid var(--ev-accent, var(--astra-primary));
  border-radius: 5px;
  background: var(--ev-bg, var(--astra-bg-panel));
  backdrop-filter: blur(2px);
  color: var(--astra-text-primary);
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  transition: background-color 0.12s ease, box-shadow 0.12s ease, z-index 0.12s ease;
}

.week-event.is-compact {
  padding: 2px 4px;
}

.week-event:hover,
.week-event.is-linked {
  z-index: 5;
  background: var(--ev-bg-hover, var(--astra-bg-elevated));
  box-shadow: 0 2px 8px rgba(9, 9, 11, 0.10), 0 0 0 1.5px var(--ev-accent, var(--astra-primary));
}

/* 多列并排紧凑卡片在悬停时舒展展示完整标题，靠近右侧边界列向左舒展 */
.week-event.is-compact:hover {
  z-index: 12;
  width: auto !important;
  min-width: 148px;
  max-width: 220px;
  background: var(--astra-bg-panel);
  box-shadow: 0 6px 16px rgba(9, 9, 11, 0.14), 0 0 0 1.5px var(--ev-accent, var(--astra-primary));
}

.week-event.is-compact.is-right-edge:hover {
  left: auto !important;
  right: 2px;
}

.week-event.is-paused {
  border: 1px dashed var(--astra-border-hover);
  border-left: 3px dashed var(--astra-neutral);
  background: color-mix(in srgb, var(--astra-bg-subtle) 92%, white);
  color: var(--astra-text-muted);
}

.week-event.is-paused.is-linked {
  box-shadow: 0 0 0 1.5px var(--astra-neutral);
}

.week-event__line {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  line-height: 1.2;
}

.week-event__time {
  flex: none;
  color: var(--ev-text, var(--astra-text-secondary));
  font-family: var(--astra-font-mono);
  font-size: 10.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.week-event.is-paused .week-event__time {
  color: var(--astra-text-muted);
}

.week-event__title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 11px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.week-event__emp {
  color: var(--astra-text-muted);
  font-size: 10.5px;
  line-height: 1.15;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (min-width: 2240px) {
  .week-cal__gutter-head,
  .week-cal__strip-gutter,
  .week-cal__axis {
    width: 68px;
  }
  .week-axis-label {
    padding-right: 10px;
    font-size: 11.5px;
  }
  .week-day-head {
    padding: 8px 11px 9px;
  }
  .week-day-head__dow {
    font-size: 12px;
  }
  .week-day-head__num {
    min-width: 26px;
    height: 26px;
    font-size: 14px;
  }
  .week-cal__strip {
    max-height: 132px;
  }
  .strip-pill {
    height: 23px;
    padding: 0 7px;
    font-size: 11.5px;
  }
  .week-event {
    padding: 4px 7px;
  }
  .week-event.is-compact:hover {
    min-width: 180px;
    max-width: 300px;
  }
  .week-event__time {
    font-size: 11px;
  }
  .week-event__title {
    font-size: 12px;
  }
  .week-event__emp {
    font-size: 11px;
  }
}
</style>
