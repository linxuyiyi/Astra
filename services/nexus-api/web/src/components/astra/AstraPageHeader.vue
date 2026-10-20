<template>
  <header
    class="astra-page-header"
    :class="{ 'astra-page-header--compact': hideTitle }"
    :aria-label="title"
    :data-testid="testid"
  >
    <div class="astra-page-header__lead">
      <h2 v-if="!hideTitle" class="astra-page-header__title">{{ title }}</h2>
      <span v-else class="astra-page-header__sr">{{ title }}</span>
      <p v-if="description" class="astra-page-header__desc">{{ description }}</p>
      <div v-if="$slots.default" class="astra-page-header__meta">
        <slot />
      </div>
    </div>
    <div class="astra-page-header__actions" :data-testid="testid ? `${testid}-actions` : undefined">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
// 所有管理页的标题区。样式全部来自 styles/page-layout.css 的 .astra-page-header。
// hideTitle 只隐藏视觉大标题，title 仍作为 aria-label，actions / default 插槽不变。
defineProps<{ title: string; description?: string; testid?: string; hideTitle?: boolean }>()
</script>
