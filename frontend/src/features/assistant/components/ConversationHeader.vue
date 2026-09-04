<template>
  <div class="conversation-header">
    <div class="title">NEXUS 助手</div>
    <div class="actions">
      <button class="action-btn" @click="$emit('new-conversation')">新对话</button>
      <button class="close-btn" @click="$emit('close')">×</button>
    </div>
  </div>
</template>

<script setup lang="ts">
defineEmits<{ (e: 'close'): void, (e: 'new-conversation'): void }>()
</script>

<style scoped>
.conversation-header {
  padding: 16px;
  border-bottom: 1px solid #eee;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.actions {
  display: flex;
  gap: 8px;
}
</style>
