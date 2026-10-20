<template>
  <ElDialog
    v-if="open"
    :model-value="true"
    data-testid="directory-picker-dialog"
    class="directory-picker-dialog"
    :title="title"
    width="min(520px, 100%)"
    :close-on-click-modal="false"
    :before-close="requestClose"
  >
    <ElText v-if="subtitle" type="info">{{ subtitle }}</ElText>
    <div class="directory-picker">
      <ElInput v-model="query" :prefix-icon="Search" clearable placeholder="搜索知识空间或目录" aria-label="搜索目录" />
      <ElTree
        ref="treeRef"
        :data="spaces"
        node-key="id"
        default-expand-all
        :expand-on-click-node="false"
        highlight-current
        :current-node-key="pickedId"
        :props="treeProps"
        :filter-node-method="filterNode"
        empty-text="没有匹配的目录"
        @node-click="onNodeClick"
      >
        <template #default="{ data }">
          <span class="directory-node">
            <Folder :size="15" />
            <span class="directory-node__copy">
              <strong :title="data.name">{{ data.name }}</strong>
              <small v-if="query">{{ pathById.get(data.id) }}</small>
            </span>
            <span class="directory-node__count">{{ data.knowledge_count }}</span>
          </span>
        </template>
      </ElTree>
    </div>

    <template #footer>
      <ElButton :disabled="busy" @click="emit('close')">取消</ElButton>
      <ElButton type="primary" :loading="busy" :disabled="!pickedId" @click="confirm">{{ confirmLabel }}</ElButton>
    </template>
  </ElDialog>
</template>

<script setup lang="ts">
import { Folder, Search } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'
import { ElButton, ElDialog, ElInput, ElText, ElTree } from 'element-plus'
import type { TreeNodeData } from 'element-plus'
import type { KnowledgeSpace } from '@/api/types'

const props = withDefaults(defineProps<{
  open: boolean
  title: string
  subtitle?: string
  spaces: KnowledgeSpace[]
  selectedId?: string
  excludeIds?: string[]
  confirmLabel?: string
  busy?: boolean
}>(), {
  subtitle: '',
  selectedId: '',
  excludeIds: () => [],
  confirmLabel: '确定',
  busy: false,
})

const emit = defineEmits<{ close: []; confirm: [id: string] }>()
const treeRef = ref<InstanceType<typeof ElTree> | null>(null)
const query = ref('')
const pickedId = ref('')

const excluded = computed(() => new Set(props.excludeIds))
const treeProps = { disabled: (data: TreeNodeData) => excluded.value.has((data as KnowledgeSpace).id) }
const pathById = computed(() => {
  const result = new Map<string, string>()
  const walk = (nodes: KnowledgeSpace[], parents: string[] = []) => {
    for (const node of nodes) {
      const path = [...parents, node.name]
      result.set(node.id, path.join(' / '))
      walk(node.children ?? [], path)
    }
  }
  walk(props.spaces)
  return result
})

watch(() => props.open, (open) => {
  if (!open) return
  query.value = ''
  pickedId.value = props.selectedId && !excluded.value.has(props.selectedId) ? props.selectedId : ''
}, { immediate: true })
watch(query, (value) => { treeRef.value?.filter(value) })

function filterNode(value: string, data: TreeNodeData): boolean {
  if (!value) return true
  const space = data as KnowledgeSpace
  const keyword = value.toLocaleLowerCase()
  return space.name.toLocaleLowerCase().includes(keyword) || (pathById.value.get(space.id) ?? '').toLocaleLowerCase().includes(keyword)
}
function onNodeClick(data: KnowledgeSpace) {
  if (excluded.value.has(data.id)) {
    treeRef.value?.setCurrentKey(pickedId.value || null)
    return
  }
  pickedId.value = data.id
}
function requestClose(): void {
  if (!props.busy) emit('close')
}
function confirm(): void {
  if (!pickedId.value || excluded.value.has(pickedId.value)) return
  emit('confirm', pickedId.value)
}
</script>

<style>
.directory-picker-dialog .directory-picker{display:grid;gap:12px}
.directory-picker-dialog .directory-picker .el-tree{--el-tree-node-content-height:38px;max-height:min(430px,55vh);overflow:auto;padding:5px;border:1px solid var(--astra-border);border-radius:10px;background:#fbfdfc}
.directory-picker-dialog .directory-node{display:flex;align-items:center;gap:8px;min-width:0;flex:1}
.directory-picker-dialog .directory-node__copy{display:grid;min-width:0;flex:1}
.directory-picker-dialog .directory-node__copy strong,.directory-picker-dialog .directory-node__copy small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.directory-picker-dialog .directory-node__copy strong{font-size:var(--astra-font-body)}
.directory-picker-dialog .directory-node__copy small{margin-top:2px;color:var(--astra-text-muted);font-size:var(--astra-font-helper)}
.directory-picker-dialog .directory-node__count{flex:none;color:var(--astra-text-muted);font-size:var(--astra-font-helper)}
</style>
