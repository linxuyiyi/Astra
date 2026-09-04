<template>
  <div class="business-card employee-card">
    <div class="title">{{ data.name }}</div>
    <div class="status">{{ data.status }}</div>
  </div>
</template>
<script setup lang="ts">
defineProps<{ data: any }>()
</script>
<style scoped>.business-card { border: 1px solid #ddd; padding: 12px; border-radius: 4px; margin-top: 8px; }</style>
