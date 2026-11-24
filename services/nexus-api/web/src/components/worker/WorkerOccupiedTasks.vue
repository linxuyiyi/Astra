<template>
  <ul v-if="tasks.length" class="occupied-tasks">
    <li v-for="task in tasks" :key="task.id" class="occupied-tasks__item">
      <RouterLink
        class="occupied-chip"
        :to="{ name: 'task-detail', params: { id: task.id } }"
        :title="`${task.title} (${task.id})`"
        @click.stop
      >
        <span class="occupied-chip__dot" aria-hidden="true" />
        <span class="occupied-chip__title">{{ task.title }}</span>
        <span class="occupied-chip__id">{{ shortId(task.id) }}</span>
        <ArrowUpRight class="occupied-chip__arrow" :size="12" aria-hidden="true" />
      </RouterLink>
    </li>
  </ul>
  <span v-else class="occupied-empty">无占用</span>
</template>

<script setup lang="ts">
import { ArrowUpRight } from 'lucide-vue-next'
import { RouterLink } from 'vue-router'
import type { WorkerOccupiedTask } from '@/api/types'

defineProps<{ tasks: WorkerOccupiedTask[] }>()

function shortId(value: string): string {
  if (!value) return ''
  return value.length > 10 ? value.slice(0, 8) : value
}
</script>

<style scoped>
.occupied-tasks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
  min-width: 0;
}

.occupied-tasks__item {
  min-width: 0;
  max-width: 100%;
}

.occupied-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  min-width: 0;
  padding: 3px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-primary);
  font-size: 12.5px;
  line-height: 18px;
  text-decoration: none;
  transition: border-color 0.15s ease, background-color 0.15s ease, color 0.15s ease;
}

.occupied-chip:hover {
  border-color: var(--astra-accent-border);
  background: var(--astra-accent-soft);
}

.occupied-chip:focus-visible {
  outline: 2px solid var(--astra-primary);
  outline-offset: 1px;
}

.occupied-chip__dot {
  width: 6px;
  height: 6px;
  flex: none;
  border-radius: 50%;
  background: var(--astra-info);
}

.occupied-chip__title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
  color: var(--astra-accent-text);
}

.occupied-chip:hover .occupied-chip__title {
  color: var(--astra-accent-hover);
}

.occupied-chip__id {
  flex: none;
  padding-left: 6px;
  border-left: 1px solid var(--astra-border);
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.occupied-chip__arrow {
  flex: none;
  color: var(--astra-text-muted);
  transition: transform 0.15s ease, color 0.15s ease;
}

.occupied-chip:hover .occupied-chip__arrow {
  color: var(--astra-accent-hover);
  transform: translate(1px, -1px);
}

.occupied-empty {
  color: var(--astra-text-muted);
  font-size: var(--astra-font-helper);
}
</style>
