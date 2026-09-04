<template>
  <div class="business-card task-card">
    <div class="card-header">
      <span class="type">Task</span>
      <span class="id">{{ data.id }}</span>
    </div>
    <div class="title">{{ data.title }}</div>
    <div class="status">{{ data.status }}</div>
    <!-- Render fields and actions -->
  </div>
</template>
<script setup lang="ts">
defineProps<{ data: any }>()
</script>
<style scoped>.business-card { border: 1px solid #ddd; padding: 12px; border-radius: 4px; margin-top: 8px; }</style>
