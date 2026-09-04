<template>
  <div class="context-bar" v-if="contextActive">
    <span>当前上下文: {{ contextLabel }}</span>
    <button @click="clearContext">×</button>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
const contextActive = ref(true)
const contextLabel = ref("无")
const clearContext = () => { contextActive.value = false }
</script>

<style scoped>
.context-bar {
  padding: 8px 16px;
  background-color: #e6f7ff;
  border-top: 1px solid #eee;
  display: flex;
  justify-content: space-between;
  font-size: 12px;
}
</style>
