<template>
  <div class="assistant-launcher" @click="toggleDrawer">
    <button class="launcher-button">NEXUS 助手</button>
  </div>
  <AssistantDrawer v-model:visible="isDrawerVisible" />
</template>

<script setup lang="ts">
import { ref } from 'vue'
import AssistantDrawer from './AssistantDrawer.vue'

const isDrawerVisible = ref(false)
const toggleDrawer = () => {
  isDrawerVisible.value = !isDrawerVisible.value
}
</script>

<style scoped>
.assistant-launcher {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 1000;
}
.launcher-button {
  padding: 12px 24px;
  border-radius: 24px;
  background-color: var(--color-primary);
  color: white;
  border: none;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
</style>
