<template>
  <div class="month-cal" data-testid="schedules-calendar-month">
    <div class="month-cal__scroll">
      <div class="month-cal__weekdays" role="row">
        <div
          v-for="(label, idx) in MONDAY_FIRST_WEEKDAYS"
          :key="label"
          class="month-cal__weekday"
          :class="{ 'is-weekend': idx >= 5 }"
          role="columnheader"
        >
          {{ label }}
        </div>
      </div>

      <div
        ref="gridRef"
        class="month-cal__grid"
        :style="{ '--month-weeks': String(weekCount) }"
        role="grid"
        aria-label="月历日程视图"
      >
        <div
          v-for="cell in cells"
          :key="cell.ymd"
          class="month-cell"
          :class="{
            'is-outside': !cell.isCurrentMonth,
            'is-today': cell.isToday,
            'is-weekend': cell.isWeekend,
            'is-selected': cell.ymd === selectedYmd,
          }"
          role="gridcell"
          :aria-selected="cell.ymd === selectedYmd"
          :data-ymd="cell.ymd"
          @click="emit('select-date', cell.ymd)"
        >
          <div class="month-cell__head">
            <span class="month-cell__date" :class="{ 'is-today-badge': cell.isToday }">
              <span class="month-cell__date-full">{{ cellDateFull(cell) }}</span>
              <span class="month-cell__date-short">{{ cellDateShort(cell) }}</span>
            </span>
            <div class="month-cell__head-right">
              <span
                v-if="dayItems(cell.ymd).length > 0"
                class="month-cell__count"
                :title="`当日共 ${dayItems(cell.ymd).length} 项日程`"
              >
                {{ dayItems(cell.ymd).length }}
              </span>
              <button
                type="button"
                class="month-cell__add"
                :aria-label="`在 ${cell.ymd} 新建日程`"
                title="以此日期新建日程"
                @click.stop="emit('create-at', { ymd: cell.ymd })"
              >
                <Plus :size="13" aria-hidden="true" />
              </button>
            </div>
          </div>

          <div class="month-cell__events">
            <button
              v-for="item in visibleItems(cell.ymd)"
              :key="`${cell.ymd}-${item.schedule.id}`"
              type="button"
              class="cal-pill"
              :class="{
                'is-paused': !item.schedule.data.enabled,
                'is-linked': hoveredScheduleId === item.schedule.id,
              }"
              :style="pillStyle(item)"
              :title="pillTooltip(item)"
              @mouseenter="emit('hover-schedule', item.schedule.id)"
              @mouseleave="emit('hover-schedule', '')"
              @click.stop="handlePillClick(cell.ymd, item.schedule)"
            >
              <Repeat
                v-if="item.isHighFrequency"
                :size="11"
                class="cal-pill__repeat"
                aria-hidden="true"
              />
              <span class="cal-pill__time">{{ item.compactTimeLabel }}</span>
              <span class="cal-pill__title">{{ item.schedule.data.name }}</span>
            </button>

            <button
              v-if="overflowCount(cell.ymd) > 0"
              type="button"
              class="month-cell__more"
              :title="`在当日排程详情查看 ${cell.ymd} 全部 ${dayItems(cell.ymd).length} 项日程`"
              @click.stop="emit('inspect-date', cell.ymd)"
            >
              +{{ overflowCount(cell.ymd) }} 项
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useResizeObserver } from '@vueuse/core';
import { computed, ref } from 'vue';
import { Plus, Repeat } from 'lucide-vue-next';
import type { EmployeeSchedule } from '@/api';
import {
  EMPLOYEE_TONES,
  MONDAY_FIRST_WEEKDAYS,
  type CalendarCell,
  type CalendarDaySchedule,
} from '@/features/schedules/cron';

const props = defineProps<{
  cells: CalendarCell[];
  dayMap: Map<string, CalendarDaySchedule[]>;
  selectedYmd: string;
  hoveredScheduleId: string;
}>();

const emit = defineEmits<{
  'select-date': [ymd: string];
  'inspect-date': [ymd: string];
  'open-schedule': [schedule: EmployeeSchedule];
  'create-at': [payload: { ymd: string; hour?: number }];
  'hover-schedule': [scheduleId: string];
}>();

const gridRef = ref<HTMLElement | null>(null);
const gridHeight = ref(0);
useResizeObserver(gridRef, entries => {
  gridHeight.value = entries[0]?.contentRect.height ?? 0;
});

const weekCount = computed(() => Math.max(4, Math.ceil(props.cells.length / 7)));
const maxVisiblePerCell = computed(() => {
  const base = weekCount.value >= 6 ? 3 : 4;
  if (!gridHeight.value) return base;
  const cellHeight = gridHeight.value / weekCount.value;
  if (cellHeight < 165) return base;
  return Math.min(7, Math.max(base, Math.floor((cellHeight - 48) / 28)));
});

function dayItems(ymd: string): CalendarDaySchedule[] {
  return props.dayMap.get(ymd) ?? [];
}

function visibleItems(ymd: string): CalendarDaySchedule[] {
  const list = dayItems(ymd);
  const cap = maxVisiblePerCell.value;
  if (list.length <= cap) return list;
  // 为底部 "+N 项" 腾出 1 行位置，防止 6 周布局撑高单元格
  return list.slice(0, Math.max(2, cap - 1));
}

function overflowCount(ymd: string): number {
  const total = dayItems(ymd).length;
  const shown = visibleItems(ymd).length;
  return Math.max(0, total - shown);
}

function cellDateFull(cell: CalendarCell): string {
  if (cell.day === 1) return `${cell.month}月1日`;
  return String(cell.day);
}

function cellDateShort(cell: CalendarCell): string {
  if (cell.day === 1) return `${cell.month}/1`;
  return String(cell.day);
}

function pillStyle(item: CalendarDaySchedule): Record<string, string> {
  if (!item.schedule.data.enabled) return {};
  const tone = EMPLOYEE_TONES[item.toneIndex] ?? EMPLOYEE_TONES[0]!;
  return {
    '--pill-accent': tone.accent,
    '--pill-bg': tone.bg,
    '--pill-bg-hover': tone.bgHover,
    '--pill-border': tone.border,
    '--pill-text': tone.text,
  };
}

function pillTooltip(item: CalendarDaySchedule): string {
  const status = item.schedule.data.enabled ? '已启用' : '已暂停';
  return `${item.schedule.data.name}\n员工：${item.schedule.employee_name}\n当日：${item.detailTimeLabel}\n规则：${item.ruleLabel} · ${status}`;
}

function handlePillClick(ymd: string, schedule: EmployeeSchedule) {
  emit('select-date', ymd);
  emit('open-schedule', schedule);
}
</script>

<style scoped>
.month-cal {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--astra-bg-panel);
}

/* 表头与网格共享同一滚动容器，彻底消除滚动条宽度导致的列边界错位 */
.month-cal__scroll {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}

.month-cal__weekdays {
  position: sticky;
  top: 0;
  z-index: 4;
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  flex: none;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}

.month-cal__weekday {
  padding: 7px 10px;
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 600;
  text-align: left;
  white-space: nowrap;
  border-right: 1px solid var(--astra-border-subtle);
}

.month-cal__weekday:last-child {
  border-right: 0;
}

.month-cal__weekday.is-weekend {
  color: var(--astra-text-muted);
}

.month-cal__grid {
  flex: 1 1 auto;
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  grid-template-rows: repeat(var(--month-weeks, 5), minmax(104px, 1fr));
}

.month-cell {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  min-height: 0;
  padding: 5px 6px 6px;
  border-right: 1px solid var(--astra-border-subtle);
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-panel);
  cursor: pointer;
  overflow: hidden;
  transition: background-color 0.14s ease, box-shadow 0.14s ease;
}

.month-cell:nth-child(7n) {
  border-right: 0;
}

.month-cell.is-weekend {
  background: color-mix(in srgb, var(--astra-bg-subtle) 65%, var(--astra-bg-panel));
}

.month-cell.is-outside {
  background: var(--astra-bg-subtle);
}

.month-cell.is-outside .month-cell__date {
  color: #a1a1aa;
}

.month-cell.is-outside .month-cell__events {
  opacity: 0.56;
}

.month-cell:hover {
  background: var(--astra-bg-base);
}

.month-cell.is-selected {
  background: color-mix(in srgb, var(--astra-accent-soft) 55%, var(--astra-bg-panel));
  box-shadow: inset 0 0 0 1.5px var(--astra-primary);
  z-index: 1;
}

.month-cell__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 2px;
  min-height: 22px;
  flex: none;
  min-width: 0;
}

.month-cell__date {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  min-width: 22px;
  height: 22px;
  padding: 0 5px;
  border-radius: 999px;
  color: var(--astra-text-primary);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  white-space: nowrap;
}

.month-cell__date-short {
  display: none;
}

.month-cell__date.is-today-badge {
  background: var(--astra-primary);
  color: #ffffff;
}

.month-cell__head-right {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: none;
}

.month-cell__count {
  color: var(--astra-text-muted);
  font-size: 11px;
  font-family: var(--astra-font-mono);
  font-variant-numeric: tabular-nums;
  padding: 0 3px;
  white-space: nowrap;
}

.month-cell__add {
  display: inline-grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--astra-text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.14s ease, background-color 0.14s ease, color 0.14s ease, border-color 0.14s ease;
}

.month-cell:hover .month-cell__add,
.month-cell.is-selected .month-cell__add,
.month-cell__add:focus-visible {
  opacity: 1;
}

.month-cell__add:hover {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
}

.month-cell__events {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
}

.cal-pill {
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  min-width: 0;
  height: 21px;
  padding: 0 6px 0 5px;
  border: 1px solid var(--pill-border, var(--astra-border));
  border-left: 3px solid var(--pill-accent, var(--astra-primary));
  border-radius: 5px;
  background: var(--pill-bg, var(--astra-bg-subtle));
  color: var(--astra-text-primary);
  font-size: 11.5px;
  line-height: 1;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.12s ease, box-shadow 0.12s ease, transform 0.12s ease;
}

.cal-pill:hover {
  background: var(--pill-bg-hover, var(--astra-bg-elevated));
}

.cal-pill.is-linked {
  background: var(--pill-bg-hover, var(--astra-bg-elevated));
  box-shadow: 0 0 0 1.5px var(--pill-accent, var(--astra-primary));
}

.cal-pill.is-paused {
  border: 1px dashed var(--astra-border-hover);
  border-left: 3px dashed var(--astra-neutral);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
}

.cal-pill.is-paused .cal-pill__time {
  color: var(--astra-text-muted);
}

.cal-pill.is-paused.is-linked {
  box-shadow: 0 0 0 1.5px var(--astra-neutral);
}

.cal-pill__repeat {
  flex: none;
  color: var(--pill-text, var(--astra-text-muted));
  opacity: 0.85;
}

.cal-pill__time {
  flex: none;
  color: var(--pill-text, var(--astra-text-secondary));
  font-family: var(--astra-font-mono);
  font-size: 10.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.cal-pill__title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}

.month-cell__more {
  align-self: flex-start;
  max-width: 100%;
  padding: 1px 5px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--astra-accent-text);
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.month-cell__more:hover {
  background: var(--astra-accent-soft);
}

@media (min-width: 2240px) {
  .month-cal__weekday {
    padding: 9px 12px;
    font-size: 12.5px;
  }
  .month-cal__grid {
    grid-template-rows: repeat(var(--month-weeks, 5), minmax(136px, 1fr));
  }
  .month-cell {
    padding: 7px 8px 8px;
    gap: 4px;
  }
  .month-cell__date {
    min-width: 24px;
    height: 24px;
    padding: 0 6px;
    font-size: 12.5px;
  }
  .month-cell__events {
    gap: 4px;
  }
  .cal-pill {
    height: 24px;
    padding: 0 7px 0 6px;
    gap: 5px;
    font-size: 12px;
  }
  .cal-pill__time {
    font-size: 11px;
  }
  .month-cell__more {
    font-size: 11.5px;
  }
}

@media (max-width: 960px) {
  .month-cell__date-full {
    display: none;
  }
  .month-cell__date-short {
    display: inline;
  }
}

@media (max-width: 640px) {
  .month-cal__weekday {
    padding: 6px 4px;
    text-align: center;
    font-size: 11px;
  }
  .month-cal__grid {
    grid-template-rows: repeat(var(--month-weeks, 5), minmax(88px, 1fr));
  }
  .month-cell {
    padding: 4px 3px;
    gap: 2px;
  }
  .month-cell__date {
    min-width: 20px;
    height: 20px;
    padding: 0 4px;
    font-size: 11px;
  }
  .month-cell__head-right {
    display: none;
  }
  .cal-pill {
    height: 19px;
    padding: 0 3px;
    gap: 2px;
    font-size: 10.5px;
  }
  .cal-pill__repeat {
    display: none;
  }
  .cal-pill__time {
    font-size: 9.5px;
  }
  .month-cell__more {
    padding: 0 2px;
    font-size: 10px;
  }
}
</style>
