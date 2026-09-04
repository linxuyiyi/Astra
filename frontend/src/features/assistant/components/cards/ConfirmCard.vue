<template>
  <div class="business-card confirm-card">
    <div class="title">Please Confirm</div>
    <div class="preview-data">
      <pre>{{ data.preview }}</pre>
    </div>
    <div class="actions">
       <button @click="$emit('confirm')">Confirm</button>
       <button @click="$emit('cancel')">Cancel</button>
    </div>
  </div>
</template>
<script setup lang="ts">
defineProps<{ data: any }>()
defineEmits<{ (e: 'confirm'): void, (e: 'cancel'): void }>()
</script>
<style scoped>.business-card { border: 1px solid #ddd; padding: 12px; border-radius: 4px; margin-top: 8px; }</style>
