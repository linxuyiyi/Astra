<template>
  <aside class="day-inspector" data-testid="schedules-day-inspector" aria-label="当日排程详情">
    <div class="day-inspector__head">
      <div class="day-inspector__date-group">
        <div class="day-inspector__title-row">
          <h3 class="day-inspector__date">{{ headingDate }}</h3>
          <span class="day-inspector__dow">{{ headingWeekday }}</span>
          <span v-if="isToday" class="day-inspector__today">今天</span>
        </div>
        <div class="day-inspector__summary">
          <template v-if="items.length > 0">
            <span>共 <b>{{ items.length }}</b> 项日程</span>
            <span class="sep">·</span>
            <span>{{ enabledCount }} 启用</span>
            <span v-if="pausedCount > 0"> / {{ pausedCount }} 暂停</span>
          </template>
          <span v-else>当日无计划触发</span>
        </div>
      </div>
      <ElButton
        size="small"
        :icon="Plus"
        data-testid="inspector-create"
        @click="emit('create-at', { ymd: selectedYmd })"
      >
        新建
      </ElButton>
    </div>

    <div class="day-inspector__body">
      <div v-if="!items.length" class="day-inspector__empty">
        <CalendarDays :size="28" class="day-inspector__empty-icon" aria-hidden="true" />
        <p class="day-inspector__empty-title">这一天暂无安排</p>
        <p class="day-inspector__empty-desc">
          {{ hasFilters ? '可调整上方筛选条件，或为这一天新建周期日程。' : '点击下方按钮为数字员工安排周期任务。' }}
        </p>
        <ElButton type="primary" size="small" :icon="Plus" @click="emit('create-at', { ymd: selectedYmd })">
          在此日新建日程
        </ElButton>
      </div>

      <template v-else>
        <!-- 定时触发区 -->
        <section v-if="timedItems.length" class="inspector-section">
          <div class="inspector-section__cap">
            <span>定时触发</span>
            <span class="inspector-section__badge">{{ timedItems.length }}</span>
          </div>

          <article
            v-for="item in timedItems"
            :key="item.schedule.id"
            class="agenda-card"
            :class="{
              'is-paused': !item.schedule.data.enabled,
              'is-linked': hoveredScheduleId === item.schedule.id,
            }"
            :style="cardStyle(item)"
            @mouseenter="emit('hover-schedule', item.schedule.id)"
            @mouseleave="emit('hover-schedule', '')"
          >
            <div class="agenda-card__top" @click="emit('open-history', item.schedule)">
              <div class="agenda-card__time-row">
                <span class="agenda-card__times">{{ item.times.join(' / ') }}</span>
                <span class="agenda-card__status" :class="item.schedule.data.enabled ? 'is-on' : 'is-off'">
                  {{ item.schedule.data.enabled ? '启用' : '暂停' }}
                </span>
              </div>

              <div class="agenda-card__title" :title="item.schedule.data.name">
                {{ item.schedule.data.name }}
              </div>

              <div class="agenda-card__meta">
                <span class="agenda-card__emp">
                  <i class="emp-dot" :style="{ background: toneAccent(item.toneIndex) }" aria-hidden="true" />
                  <span>{{ item.schedule.employee_name }}</span>
                </span>
                <span class="sep">·</span>
                <span class="agenda-card__rule">{{ item.ruleLabel }}</span>
              </div>

              <div v-if="excerpt(item.schedule.data.description)" class="agenda-card__excerpt">
                {{ excerpt(item.schedule.data.description) }}
              </div>
            </div>

            <div class="agenda-card__foot">
              <ElSwitch
                size="small"
                :model-value="item.schedule.data.enabled"
                :loading="busyId === item.schedule.id"
                :disabled="busyId === item.schedule.id"
                :aria-label="`${item.schedule.data.name} 启用状态`"
                @change="emit('toggle', item.schedule, Boolean($event))"
              />
              <div class="agenda-card__actions">
                <ElButton
                  link
                  type="primary"
                  :disabled="busyId === item.schedule.id"
                  @click="emit('open-history', item.schedule)"
                >
                  记录
                </ElButton>
                <ElButton
                  link
                  type="primary"
                  :disabled="busyId === item.schedule.id"
                  @click="emit('edit', item.schedule)"
                >
                  编辑
                </ElButton>
                <ElDropdown
                  trigger="click"
                  :disabled="busyId === item.schedule.id"
                  @command="cmd => handleCommand(item.schedule, String(cmd))"
                >
                  <ElButton
                    link
                    type="primary"
                    :disabled="busyId === item.schedule.id"
                    :aria-label="`更多操作 ${item.schedule.data.name}`"
                  >
                    更多
                  </ElButton>
                  <template #dropdown>
                    <ElDropdownMenu>
                      <ElDropdownItem command="copy-cron">复制 Cron</ElDropdownItem>
                      <ElDropdownItem command="delete" divided class="danger-item">删除日程</ElDropdownItem>
                    </ElDropdownMenu>
                  </template>
                </ElDropdown>
              </div>
            </div>
          </article>
        </section>

        <!-- 高频循环区 -->
        <section v-if="highFrequencyItems.length" class="inspector-section">
          <div class="inspector-section__cap">
            <span class="inspector-section__cap-icon">
              <Repeat :size="12" aria-hidden="true" />
              <span>当日高频循环</span>
            </span>
            <span class="inspector-section__badge">{{ highFrequencyItems.length }}</span>
          </div>

          <article
            v-for="item in highFrequencyItems"
            :key="item.schedule.id"
            class="agenda-card"
            :class="{
              'is-paused': !item.schedule.data.enabled,
              'is-linked': hoveredScheduleId === item.schedule.id,
            }"
            :style="cardStyle(item)"
            @mouseenter="emit('hover-schedule', item.schedule.id)"
            @mouseleave="emit('hover-schedule', '')"
          >
            <div class="agenda-card__top" @click="emit('open-history', item.schedule)">
              <div class="agenda-card__time-row">
                <span class="agenda-card__times">当日 {{ item.totalTriggers }} 次</span>
                <span class="agenda-card__status" :class="item.schedule.data.enabled ? 'is-on' : 'is-off'">
                  {{ item.schedule.data.enabled ? '启用' : '暂停' }}
                </span>
              </div>

              <div class="agenda-card__title" :title="item.schedule.data.name">
                {{ item.schedule.data.name }}
              </div>

              <div class="agenda-card__meta">
                <span class="agenda-card__emp">
                  <i class="emp-dot" :style="{ background: toneAccent(item.toneIndex) }" aria-hidden="true" />
                  <span>{{ item.schedule.employee_name }}</span>
                </span>
                <span class="sep">·</span>
                <span class="agenda-card__rule">{{ item.ruleLabel }}</span>
              </div>
            </div>

            <div class="hf-times">
              <button
                type="button"
                class="hf-times__toggle"
                @click="toggleExpandTimes(item.schedule.id)"
              >
                <span>{{ expandedIds.has(item.schedule.id) ? '收起时刻表' : '查看当日时刻表' }}</span>
                <ChevronDown
                  :size="13"
                  class="hf-times__chevron"
                  :class="{ 'is-open': expandedIds.has(item.schedule.id) }"
                  aria-hidden="true"
                />
              </button>
              <div v-if="expandedIds.has(item.schedule.id)" class="hf-times__grid">
                <span v-for="t in item.times" :key="t" class="hf-time-chip">{{ t }}</span>
                <span v-if="item.timesTruncated" class="hf-times__more">
                  仅列前 {{ item.times.length }} 项，共 {{ item.totalTriggers }} 次
                </span>
              </div>
            </div>

            <div class="agenda-card__foot">
              <ElSwitch
                size="small"
                :model-value="item.schedule.data.enabled"
                :loading="busyId === item.schedule.id"
                :disabled="busyId === item.schedule.id"
                :aria-label="`${item.schedule.data.name} 启用状态`"
                @change="emit('toggle', item.schedule, Boolean($event))"
              />
              <div class="agenda-card__actions">
                <ElButton
                  link
                  type="primary"
                  :disabled="busyId === item.schedule.id"
                  @click="emit('open-history', item.schedule)"
                >
                  记录
                </ElButton>
                <ElButton
                  link
                  type="primary"
                  :disabled="busyId === item.schedule.id"
                  @click="emit('edit', item.schedule)"
                >
                  编辑
                </ElButton>
                <ElDropdown
                  trigger="click"
                  :disabled="busyId === item.schedule.id"
                  @command="cmd => handleCommand(item.schedule, String(cmd))"
                >
                  <ElButton
                    link
                    type="primary"
                    :disabled="busyId === item.schedule.id"
                    :aria-label="`更多操作 ${item.schedule.data.name}`"
                  >
                    更多
                  </ElButton>
                  <template #dropdown>
                    <ElDropdownMenu>
                      <ElDropdownItem command="copy-cron">复制 Cron</ElDropdownItem>
                      <ElDropdownItem command="delete" divided class="danger-item">删除日程</ElDropdownItem>
                    </ElDropdownMenu>
                  </template>
                </ElDropdown>
              </div>
            </div>
          </article>
        </section>
      </template>
    </div>

    <div class="day-inspector__note">
      <Info :size="12" aria-hidden="true" />
      <span>按 Cron 规则展开，点击日程查看真实执行记录</span>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue';
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElSwitch } from 'element-plus';
import { CalendarDays, ChevronDown, Info, Plus, Repeat } from 'lucide-vue-next';
import type { EmployeeSchedule } from '@/api';
import {
  dayOfWeekForYmd,
  EMPLOYEE_TONES,
  parseYmd,
  WEEKDAYS,
  type CalendarDaySchedule,
} from '@/features/schedules/cron';

const props = defineProps<{
  selectedYmd: string;
  todayYmd: string;
  items: CalendarDaySchedule[];
  busyId: string;
  hoveredScheduleId: string;
  hasFilters: boolean;
}>();

const emit = defineEmits<{
  'open-history': [schedule: EmployeeSchedule];
  'edit': [schedule: EmployeeSchedule];
  'toggle': [schedule: EmployeeSchedule, enabled: boolean];
  'copy-cron': [cron: string];
  'delete': [schedule: EmployeeSchedule];
  'create-at': [payload: { ymd: string; hour?: number }];
  'hover-schedule': [scheduleId: string];
}>();

const expandedIds = reactive(new Set<string>());

const isToday = computed(() => props.selectedYmd === props.todayYmd);

const headingDate = computed(() => {
  const { month, day } = parseYmd(props.selectedYmd);
  return `${month}月${day}日`;
});

const headingWeekday = computed(() => WEEKDAYS[dayOfWeekForYmd(props.selectedYmd)] ?? '');

const enabledCount = computed(() => props.items.filter(item => item.schedule.data.enabled).length);
const pausedCount = computed(() => props.items.length - enabledCount.value);

const timedItems = computed(() => props.items.filter(item => !item.isHighFrequency));
const highFrequencyItems = computed(() => props.items.filter(item => item.isHighFrequency));

function toneAccent(index: number): string {
  return (EMPLOYEE_TONES[index] ?? EMPLOYEE_TONES[0]!).accent;
}

function cardStyle(item: CalendarDaySchedule): Record<string, string> {
  if (!item.schedule.data.enabled) return {};
  const tone = EMPLOYEE_TONES[item.toneIndex] ?? EMPLOYEE_TONES[0]!;
  return {
    '--card-accent': tone.accent,
    '--card-text': tone.text,
    '--card-bg-tint': tone.bg,
  };
}

function excerpt(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' [代码] ')
    .replace(/[#>*`_~\-\[\]()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function toggleExpandTimes(scheduleId: string) {
  if (expandedIds.has(scheduleId)) expandedIds.delete(scheduleId);
  else expandedIds.add(scheduleId);
}

function handleCommand(schedule: EmployeeSchedule, cmd: string) {
  if (cmd === 'copy-cron') emit('copy-cron', schedule.data.cron_expression);
  else if (cmd === 'delete') emit('delete', schedule);
}
</script>

<style scoped>
.day-inspector {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.day-inspector__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 14px 10px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
  flex: none;
}

.day-inspector__date-group {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.day-inspector__title-row {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.day-inspector__date {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 16px;
  font-weight: 600;
  line-height: 1.3;
  font-variant-numeric: tabular-nums;
}

.day-inspector__dow {
  color: var(--astra-text-muted);
  font-size: 12.5px;
  font-weight: 500;
}

.day-inspector__today {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--astra-primary);
  color: #ffffff;
  font-size: 10.5px;
  font-weight: 600;
}

.day-inspector__summary {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--astra-text-muted);
  font-size: 12px;
}

.day-inspector__summary b {
  color: var(--astra-text-secondary);
  font-weight: 600;
}

.sep {
  opacity: 0.5;
}

.day-inspector__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.day-inspector__empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 28px 16px;
  gap: 8px;
}

.day-inspector__empty-icon {
  color: var(--astra-border-hover);
}

.day-inspector__empty-title {
  margin: 4px 0 0;
  color: var(--astra-text-secondary);
  font-size: 13.5px;
  font-weight: 600;
}

.day-inspector__empty-desc {
  margin: 0 0 6px;
  color: var(--astra-text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.inspector-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.inspector-section__cap {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--astra-text-muted);
  font-size: 11.5px;
  font-weight: 600;
  padding: 0 2px;
}

.inspector-section__cap-icon {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.inspector-section__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 17px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
}

.agenda-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--astra-border);
  border-left: 3px solid var(--card-accent, var(--astra-primary));
  border-radius: 8px;
  background: var(--astra-bg-panel);
  overflow: hidden;
  transition: border-color 0.14s ease, box-shadow 0.14s ease, background-color 0.14s ease;
}

.agenda-card:hover,
.agenda-card.is-linked {
  border-color: var(--astra-border-hover);
  box-shadow: 0 2px 8px rgba(9, 9, 11, 0.06);
}

.agenda-card.is-paused {
  border-style: dashed;
  border-left: 3px dashed var(--astra-neutral);
  background: var(--astra-bg-subtle);
}

.agenda-card__top {
  display: grid;
  gap: 4px;
  padding: 9px 10px 8px;
  cursor: pointer;
}

.agenda-card__time-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.agenda-card__times {
  color: var(--card-text, var(--astra-text-primary));
  font-family: var(--astra-font-mono);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.agenda-card.is-paused .agenda-card__times {
  color: var(--astra-text-muted);
}

.agenda-card__status {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  padding: 0 6px;
  border-radius: 999px;
  line-height: 17px;
}

.agenda-card__status.is-on {
  background: var(--astra-success-soft);
  color: var(--astra-success);
}

.agenda-card__status.is-off {
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
}

.agenda-card__title {
  color: var(--astra-text-primary);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agenda-card.is-paused .agenda-card__title {
  color: var(--astra-text-secondary);
}

.agenda-card__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  color: var(--astra-text-muted);
  font-size: 11.5px;
}

.agenda-card__emp {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--astra-text-secondary);
  font-weight: 500;
}

.emp-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}

.agenda-card__rule {
  color: var(--astra-text-muted);
}

.agenda-card__excerpt {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.hf-times {
  border-top: 1px solid var(--astra-border-subtle);
  padding: 5px 10px;
  background: color-mix(in srgb, var(--astra-bg-subtle) 65%, var(--astra-bg-panel));
}

.hf-times__toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--astra-text-secondary);
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
}

.hf-times__toggle:hover {
  color: var(--astra-text-primary);
}

.hf-times__chevron {
  transition: transform 0.16s ease;
}

.hf-times__chevron.is-open {
  transform: rotate(180deg);
}

.hf-times__grid {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding-top: 6px;
}

.hf-time-chip {
  padding: 1px 5px;
  border: 1px solid var(--astra-border);
  border-radius: 4px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}

.hf-times__more {
  width: 100%;
  color: var(--astra-text-muted);
  font-size: 11px;
  margin-top: 2px;
}

.agenda-card__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 10px;
  border-top: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}

.agenda-card__actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.agenda-card__actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.danger-item {
  color: var(--astra-danger);
}

.day-inspector__note {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
  padding: 7px 12px;
  border-top: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 11px;
  line-height: 1.35;
}

.day-inspector__note svg {
  flex: none;
}

@media (min-width: 2240px) {
  .day-inspector__head {
    padding: 14px 16px 12px;
  }
  .day-inspector__date {
    font-size: 17px;
  }
  .day-inspector__body {
    padding: 14px 16px;
    gap: 16px;
  }
  .agenda-card__top {
    padding: 11px 12px 10px;
    gap: 5px;
  }
  .agenda-card__times {
    font-size: 12.5px;
  }
  .agenda-card__title {
    font-size: 13.5px;
  }
  .agenda-card__meta {
    font-size: 12px;
  }
  .agenda-card__excerpt {
    font-size: 12px;
    -webkit-line-clamp: 3;
  }
  .hf-time-chip {
    padding: 2px 6px;
    font-size: 11px;
  }
  .day-inspector__note {
    padding: 9px 14px;
    font-size: 11.5px;
  }
}
</style>
