<template>
  <div class="message-input-container">
    <textarea
      v-model="inputText"
      @keydown.enter.prevent="handleEnter"
      placeholder="输入内容..."
    ></textarea>
    <button @click="send" :disabled="store.isStreaming">发送</button>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useAssistantStore } from '../store/assistantStore'

const inputText = ref('')
const store = useAssistantStore()

const handleEnter = (e: KeyboardEvent) => {
  if (e.shiftKey) return;
  send()
}

const send = () => {
  if (!inputText.value.trim() || store.isStreaming) return
  store.sendMessage(inputText.value)
  inputText.value = ''
}
</script>

<style scoped>
.message-input-container {
  padding: 16px;
  border-top: 1px solid #eee;
  display: flex;
  gap: 8px;
}
textarea {
  flex: 1;
  resize: none;
  padding: 8px;
  border: 1px solid #ddd;
  border-radius: 4px;
}
button {
  padding: 0 16px;
  background-color: var(--color-primary);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
button:disabled {
  background-color: #ccc;
  cursor: not-allowed;
}
</style>
