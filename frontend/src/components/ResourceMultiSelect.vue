<script setup lang="ts">
import { computed } from 'vue'

import type { CatalogField } from '@/api/types'
import { RESOURCE_MAX } from '@/constants'
import { loadResource, resourceState } from '@/stores/resources'

const props = defineProps<{
  modelValue: string[]
  field: CatalogField
  label: string
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

const state = resourceState(props.field)

interface ResourceOption {
  value: string
  label: string
  disabled: boolean
  reason: string | null
}

const options = computed<ResourceOption[]>(() => {
  // 只有目录可用时才可断言资源真实缺失；加载中/不可用/报错时已存 ID 只是
  // “暂无法核验”，不能宣称资源已消失。
  const verifiable = state.catalog?.available === true
  const items = state.catalog?.items ?? []
  const result: ResourceOption[] = items.map((item) => {
    const selectable = item.enabled && item.selectable
    const reason = selectable
      ? null
      : item.reason || (item.enabled ? '当前版本不支持' : '未启用')
    return {
      value: item.id,
      label: item.name || item.id,
      disabled: !selectable,
      reason,
    }
  })
  const known = new Set(items.map((item) => item.id))
  for (const id of props.modelValue) {
    if (!known.has(id)) {
      result.push({
        value: id,
        label: id,
        disabled: true,
        reason: verifiable ? '资源已从目录移除' : '暂无法核验',
      })
    }
  }
  return result
})

const unavailable = computed(
  () =>
    !state.loading &&
    !state.error &&
    state.catalog !== null &&
    !state.catalog.available,
)

const empty = computed(
  () =>
    !state.loading &&
    !state.error &&
    state.catalog?.available === true &&
    state.catalog.items.length === 0,
)

function retry(): void {
  void loadResource(props.field, true)
}

function onChange(value: string[]): void {
  const seen = new Set<string>()
  const next: string[] = []
  for (const id of value) {
    if (seen.has(id)) continue
    seen.add(id)
    next.push(id)
    if (next.length >= RESOURCE_MAX) break
  }
  emit('update:modelValue', next)
}
</script>

<template>
  <div class="resource-field">
    <div class="resource-head">
      <span class="resource-label">{{ label }}</span>
      <span v-if="state.loading" class="resource-note">加载中…</span>
      <span v-else-if="state.error" class="resource-note is-error">
        {{ state.error }}
        <el-button link type="primary" size="small" @click="retry">重试</el-button>
      </span>
      <span v-else-if="unavailable" class="resource-note is-error">
        目录不可用：{{ state.catalog?.reason || '未知原因' }}
        <el-button link type="primary" size="small" @click="retry">重试</el-button>
      </span>
      <span v-else-if="empty" class="resource-note">暂无可用资源</span>
    </div>

    <el-select
      :model-value="modelValue"
      class="resource-select"
      multiple
      filterable
      collapse-tags
      collapse-tags-tooltip
      :loading="state.loading"
      :disabled="state.loading"
      :placeholder="`选择${label}`"
      @update:model-value="onChange"
    >
      <el-option
        v-for="option in options"
        :key="option.value"
        :value="option.value"
        :label="option.label"
        :disabled="option.disabled"
      >
        <span class="option-label">{{ option.label }}</span>
        <span v-if="option.reason" class="option-reason">{{ option.reason }}</span>
      </el-option>
    </el-select>
  </div>
</template>

<style scoped>
.resource-field {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 17px;
}

.resource-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.resource-label {
  font-size: 11px;
  font-weight: 750;
  color: #527263;
}

.resource-note {
  font-size: 10px;
  color: var(--muted);
}

.resource-note.is-error {
  color: var(--danger);
}

.resource-select {
  width: 100%;
}

.option-label {
  margin-right: 8px;
}

.option-reason {
  float: right;
  color: var(--muted);
  font-size: 11px;
}
</style>
