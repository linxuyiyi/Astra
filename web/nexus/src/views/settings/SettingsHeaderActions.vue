<template>
  <Teleport v-if="mounted" to="#settings-header-actions">
    <slot />
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

const mounted = ref(false)
onMounted(() => {
  mounted.value = true
})
</script>
