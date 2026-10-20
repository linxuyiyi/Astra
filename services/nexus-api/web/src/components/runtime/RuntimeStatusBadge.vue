<template>
  <span class="runtime-status-badge" :data-tone="meta.tone" :data-state="meta.state">
    <i v-if="showPulse" />
    <span>{{ meta.label }}</span>
    <em v-if="meta.detail && showDetail">{{ meta.detail }}</em>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  runtimeStatusMeta,
  runtimeStatusTone,
  type RuntimeStatusProjection,
  type RuntimeTone,
} from '@/features/runtime/status'

const props = withDefaults(defineProps<{
  projection: RuntimeStatusProjection
  tone?: RuntimeTone
  pulse?: boolean
  showDetail?: boolean
}>(), {
  pulse: undefined,
  showDetail: true,
})

const meta = computed(() => {
  const value = runtimeStatusMeta(props.projection)
  return { ...value, tone: props.tone ?? runtimeStatusTone(value.state) }
})
const showPulse = computed(() => props.pulse ?? meta.value.pulse)
</script>

<style scoped>
.runtime-status-badge {
  --tone: var(--astra-text-muted);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 26px;
  padding: 2px 9px;
  color: var(--tone);
  border: 1px solid color-mix(in srgb, var(--tone) 28%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--tone) 9%, transparent);
  font-size: var(--astra-font-helper);
  white-space: nowrap;
}
.runtime-status-badge[data-tone='info'] { --tone: var(--astra-info); }
.runtime-status-badge[data-tone='success'] { --tone: var(--astra-success); }
.runtime-status-badge[data-tone='warning'] { --tone: var(--astra-warning); }
.runtime-status-badge[data-tone='danger'] { --tone: var(--astra-danger); }
.runtime-status-badge i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: pulse 1.8s ease-in-out infinite; }
.runtime-status-badge em { padding-left: 6px; color: var(--tone); font-style: normal; opacity: .82; }
</style>
