<template>
  <div class="assistant-drawer" :class="{ visible: visible }">
    <ConversationHeader @close="$emit('update:visible', false)" />
    <MessageList />
    <ContextBar />
    <MessageInput />
  </div>
</template>

<script setup lang="ts">
import ConversationHeader from './ConversationHeader.vue'
import MessageList from './MessageList.vue'
import ContextBar from './ContextBar.vue'
import MessageInput from './MessageInput.vue'

defineProps<{ visible: boolean }>()
defineEmits<{ (e: 'update:visible', value: boolean): void }>()
</script>

<style scoped>
.assistant-drawer {
  position: fixed;
  top: 0;
  right: -420px;
  width: 420px;
  height: 100vh;
  background: white;
  box-shadow: -2px 0 8px rgba(0,0,0,0.1);
  transition: right 0.3s ease;
  z-index: 1001;
  display: flex;
  flex-direction: column;
}
.assistant-drawer.visible {
  right: 0;
}
</style>
