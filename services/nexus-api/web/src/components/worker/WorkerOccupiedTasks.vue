<template>
  <ul v-if="tasks.length" class="occupied-tasks">
    <li v-for="task in tasks" :key="task.id">
      <router-link :to="{ name: 'task-detail', params: { id: task.id } }" :title="task.title" @click.stop>{{ task.title }}</router-link>
      <span class="task-number">{{ task.id }}</span>
    </li>
  </ul>
  <span v-else class="task-number">无占用任务</span>
</template>

<script setup lang="ts">
import type { WorkerOccupiedTask } from '@/api/types'

defineProps<{ tasks: WorkerOccupiedTask[] }>()
</script>

<style scoped>
.occupied-tasks { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.occupied-tasks li { min-width: 0; }
.occupied-tasks a { margin-right: 8px; color: var(--astra-primary-bright); overflow-wrap: anywhere; }
.task-number { color: var(--astra-text-muted); font-size: var(--astra-font-helper); overflow-wrap: anywhere; }
</style>
