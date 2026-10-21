<template>
  <span class="id-display" :class="{ 'id-display--link': to }" :title="value">
    <RouterLink v-if="to" class="id-display__value" :to="to">{{ displayText }}</RouterLink>
    <span v-else class="id-display__value">{{ displayText }}</span>
    <button
      v-if="copyable"
      class="id-display__copy"
      type="button"
      :aria-label="`复制 ${value}`"
      :title="copied ? '已复制' : '复制'"
      @click.stop="copy"
    >
      <Check v-if="copied" :size="12" />
      <Copy v-else :size="12" />
    </button>
  </span>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Check, Copy } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{ value: string; to?: string; copyable?: boolean; maxWidth?: number }>(),
  { copyable: true, maxWidth: 150 },
)
const copied = ref(false)
const displayText = computed(() => {
  if (!props.value || props.value.length <= props.maxWidth) return props.value ?? '—'
  return `${props.value.slice(0, Math.floor(props.maxWidth * 0.6))}…${props.value.slice(-Math.floor(props.maxWidth * 0.3))}`
})
async function copy() {
  if (!props.value) return
  try {
    await navigator.clipboard.writeText(props.value)
  } catch {
    const area = document.createElement('textarea')
    area.value = props.value
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    document.body.removeChild(area)
  }
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1200)
}
</script>

<style scoped>
.id-display {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  font-family: var(--astra-font-family);
  font-size: var(--astra-font-helper);
}
.id-display__value {
  display: inline-block;
  min-width: 0;
  max-width: 180px;
  overflow: hidden;
  color: var(--astra-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.id-display--link .id-display__value { color: var(--astra-primary-bright); cursor: pointer; }
.id-display--link:hover .id-display__value { text-decoration: underline; }
.id-display__copy {
  display: grid;
  place-items: center;
  flex: none;
  width: 18px;
  height: 18px;
  padding: 0;
  color: var(--astra-text-muted);
  border: 0;
  border-radius: 5px;
  background: transparent;
  cursor: pointer;
}
.id-display__copy:hover { color: var(--astra-primary-bright); background: var(--astra-primary-soft); }
.id-display[data-size='large'] .id-display__value { max-width: 260px; font-size: var(--astra-font-control); }
</style>
