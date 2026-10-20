<template>
  <!--
    子页把自己的刷新 / 主操作按钮放进父级 API 中转的标题区右侧，
    这样网关子页的按钮与其它管理页处在同一个视觉位置。

    这里不能用裸 <Teleport to="#gateway-header-actions">：Teleport 用
    document.querySelector 解析目标，而在「从节点详情等独立整页导航回 /gateway/*」
    这条路径上，父级标题区的 DOM 此刻还没挂到 document（整棵树在同一次 patch 内
    同步挂载），目标解析必然失败，Vue 会抛 "Cannot destructure property 'type'
    of 'vnode' as it is null" 并留下空白页。

    onMounted 在整棵树挂载完成之后才执行，此后再渲染 Teleport 目标必然可解析。
    直接进入 /gateway/* 时异步组件在挂载后才 resolve，本来也不会命中这个问题。
  -->
  <Teleport v-if="mounted" to="#gateway-header-actions">
    <slot />
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

const mounted = ref(false)
onMounted(() => { mounted.value = true })
</script>
