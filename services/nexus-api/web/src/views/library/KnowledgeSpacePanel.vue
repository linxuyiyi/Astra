<template>
  <aside class="space-panel">
    <header class="space-panel__header">
      <div><h3>知识空间</h3><p>选择空间或目录查看知识</p></div>
      <button v-if="canManage" type="button" title="新建知识空间" aria-label="新建知识空间" @click="beginCreate(null)"><Plus :size="15" /></button>
    </header>

    <ElInput
      v-model="searchQuery"
      class="space-panel__search"
      :prefix-icon="Search"
      placeholder="搜索知识空间"
      aria-label="搜索知识空间"
      clearable
    />

    <nav aria-label="知识空间目录树">
      <button
        type="button"
        class="space-all"
        :class="{ selected: selectedId === null }"
        :title="`当前身份可见的全部知识 ${total} 条（含未归入目录的知识）`"
        @click="select(null)"
      >
        <span><Layers3 :size="16" />全部知识</span><strong>{{ total }}</strong>
      </button>

      <ElTree
        :data="visibleSpaces"
        node-key="id"
        :expand-on-click-node="false"
        highlight-current
        :current-node-key="selectedId ?? undefined"
        :default-expanded-keys="expandedForTree"
        :empty-text="emptyText"
        @node-click="onNodeClick"
        @node-expand="onNodeExpand"
        @node-collapse="onNodeCollapse"
      >
        <template #default="{ data }">
          <span class="space-node">
            <Folder :size="15" class="space-node__icon" />
            <span class="space-node__name" :title="data.name">{{ data.name }}</span>
            <strong class="space-node__count" :title="`当前目录可见 ${data.subtree_knowledge_count} 条知识（含子目录；本目录直接 ${data.knowledge_count} 条）`">{{ data.subtree_knowledge_count }}</strong>
            <ElDropdown v-if="canManage" trigger="click" placement="bottom-end" @command="(command: string) => runCommand(command, data)">
              <button type="button" class="space-node__menu" :aria-label="`管理目录 ${data.name}`" @click.stop><MoreHorizontal :size="14" /></button>
              <template #dropdown>
                <ElDropdownMenu>
                  <ElDropdownItem command="create">新建子目录</ElDropdownItem>
                  <ElDropdownItem command="rename">重命名</ElDropdownItem>
                  <ElDropdownItem v-if="data.parent_id" command="move">移动到…</ElDropdownItem>
                  <ElDropdownItem command="delete" divided class="space-node__menu-danger">删除</ElDropdownItem>
                </ElDropdownMenu>
              </template>
            </ElDropdown>
          </span>
        </template>
      </ElTree>
    </nav>

    <ElDialog
      v-model="form.open"
      :title="formTitle"
      width="min(420px, 92%)"
      :close-on-click-modal="false"
      :close-on-press-escape="!form.submitting"
      :show-close="!form.submitting"
      destroy-on-close
      @opened="formInput?.focus()"
    >
      <ElForm @submit.prevent="submitDialog">
        <ElFormItem>
          <ElInput
            ref="formInput"
            v-model.trim="form.name"
            :aria-label="form.mode === 'rename' ? '目录名称' : form.parentId ? '子目录名称' : '知识空间名称'"
            :placeholder="form.mode === 'rename' ? '输入新名称' : '输入名称'"
            maxlength="160"
          />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton :disabled="form.submitting" @click="closeForm">取消</ElButton>
        <ElButton type="primary" :loading="form.submitting" :disabled="!form.name" @click="submitDialog">{{ form.mode === 'rename' ? '保存' : '创建' }}</ElButton>
      </template>
    </ElDialog>

    <DirectoryPickerDialog
      :open="Boolean(moveNode)"
      title="移动目录"
      :subtitle="moveNode ? `将「${moveNode.name}」移动到新的父目录。` : ''"
      :spaces="moveRoot ? [moveRoot] : []"
      :selected-id="moveNode?.parent_id ?? ''"
      :exclude-ids="moveExcludedIds"
      confirm-label="移动"
      @close="moveNode = null"
      @confirm="submitMove"
    />
  </aside>
</template>

<script setup lang="ts">
import { Folder, Layers3, MoreHorizontal, Plus, Search } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import { ElButton, ElDialog, ElDropdown, ElDropdownItem, ElDropdownMenu, ElForm, ElFormItem, ElInput, ElTree } from 'element-plus'
import type { KnowledgeSpace } from '@/api/types'
import DirectoryPickerDialog from './DirectoryPickerDialog.vue'

const props = defineProps<{ spaces: KnowledgeSpace[]; selectedId: string | null; total: number; canManage: boolean }>()
const emit = defineEmits<{
  select: [id: string | null]
  create: [name: string, parentId: string | null, done: (success: boolean) => void]
  rename: [space: KnowledgeSpace, name: string, done: (success: boolean) => void]
  move: [space: KnowledgeSpace, targetParentId: string]
  delete: [space: KnowledgeSpace]
}>()

const formInput = ref<InstanceType<typeof ElInput> | null>(null)
const searchQuery = ref('')
const expandedKeys = ref<string[]>([])
const moveNode = ref<KnowledgeSpace | null>(null)
const form = reactive({ open: false, mode: 'create' as 'create' | 'rename', parentId: null as string | null, parentName: '', target: null as KnowledgeSpace | null, name: '', submitting: false })

const visibleSpaces = computed(() => {
  const keyword = searchQuery.value.toLocaleLowerCase()
  if (!keyword) return props.spaces

  const filter = (nodes: KnowledgeSpace[]): KnowledgeSpace[] => {
    const result: KnowledgeSpace[] = []
    for (const node of nodes) {
      const children = filter(node.children ?? [])
      if (node.name.toLocaleLowerCase().includes(keyword) || children.length) result.push({ ...node, children })
    }
    return result
  }
  return filter(props.spaces)
})
const emptyText = computed(() => searchQuery.value ? '没有匹配的知识空间' : '暂无知识空间')

// 搜索时命中路径一次性并入强制展开集合；用户在搜索期间的折叠对两个集合都生效，
// 清空搜索后回到用户自己的 expandedKeys，不污染用户状态。
const forcedKeys = ref<string[]>([])
const expandedForTree = computed(() => {
  if (!searchQuery.value) return expandedKeys.value
  return [...new Set([...expandedKeys.value, ...forcedKeys.value])]
})
watch(searchQuery, (query) => {
  if (!query) {
    forcedKeys.value = []
    return
  }
  const keys: string[] = []
  const collect = (nodes: KnowledgeSpace[]) => {
    for (const node of nodes) {
      const children = node.children ?? []
      if (children.length) {
        keys.push(node.id)
        collect(children)
      }
    }
  }
  collect(visibleSpaces.value)
  forcedKeys.value = keys
})
const moveRoot = computed(() => moveNode.value ? findRoot(props.spaces, moveNode.value.id) : null)
const moveExcludedIds = computed(() => {
  if (!moveNode.value) return []
  return [...descendantIds(moveNode.value), moveNode.value.id]
})

// 选中项必须可见：把它的所有父目录并入展开集合（UI 状态，不是业务规则）。
watch(() => props.selectedId, (id) => {
  if (!id) return
  const ancestors = ancestorKeys(props.spaces, id)
  if (!ancestors.length) return
  const next = new Set(expandedKeys.value)
  for (const key of ancestors) next.add(key)
  expandedKeys.value = [...next]
})

function select(id: string | null) { emit('select', id) }
function onNodeClick(data: KnowledgeSpace) { emit('select', data.id) }
function onNodeExpand(data: KnowledgeSpace) {
  if (expandedKeys.value.includes(data.id)) return
  expandedKeys.value = [...expandedKeys.value, data.id]
}
function onNodeCollapse(data: KnowledgeSpace) {
  expandedKeys.value = expandedKeys.value.filter((key) => key !== data.id)
  forcedKeys.value = forcedKeys.value.filter((key) => key !== data.id)
}

function beginCreate(parentId: string | null) {
  form.mode = 'create'
  form.parentId = parentId
  form.parentName = parentId ? findNode(props.spaces, parentId)?.name ?? '' : ''
  form.target = null
  form.name = ''
  form.submitting = false
  form.open = true
}
function beginRename(space: KnowledgeSpace) {
  form.mode = 'rename'
  form.parentId = space.parent_id
  form.target = space
  form.name = space.name
  form.submitting = false
  form.open = true
}
const formTitle = computed(() => form.mode === 'rename' ? '重命名目录' : form.parentId ? `在「${form.parentName}」下新建子目录` : '新建知识空间')
function closeForm() { form.open = false }
function submitDialog() {
  const name = form.name
  if (!name || form.submitting) return
  if (form.mode === 'rename') {
    if (!form.target) return
    if (name === form.target.name) { form.open = false; return }
    const target = form.target
    form.submitting = true
    emit('rename', target, name, (success) => {
      form.submitting = false
      if (success) form.open = false
    })
    return
  }
  const parentId = form.parentId
  form.submitting = true
  emit('create', name, parentId, (success) => {
    form.submitting = false
    if (success) form.open = false
  })
}
function openMove(space: KnowledgeSpace) { moveNode.value = space }
function submitMove(targetId: string) {
  if (!moveNode.value) return
  const node = moveNode.value
  moveNode.value = null
  if (targetId === node.parent_id) return
  const next = new Set(expandedKeys.value)
  next.add(targetId)
  for (const key of ancestorKeys(props.spaces, targetId)) next.add(key)
  expandedKeys.value = [...next]
  emit('move', node, targetId)
}

function runCommand(command: string, data: KnowledgeSpace) {
  if (command === 'create') beginCreate(data.id)
  else if (command === 'rename') beginRename(data)
  else if (command === 'move') openMove(data)
  else if (command === 'delete') emit('delete', data)
}

function ancestorKeys(nodes: KnowledgeSpace[], id: string, parents: string[] = []): string[] {
  for (const node of nodes) {
    if (node.id === id) return parents
    const found = ancestorKeys(node.children ?? [], id, [...parents, node.id])
    if (found.length) return found
  }
  return []
}
function findNode(nodes: KnowledgeSpace[], id: string | null): KnowledgeSpace | null {
  if (!id) return null
  for (const node of nodes) {
    if (node.id === id) return node
    const found = findNode(node.children ?? [], id)
    if (found) return found
  }
  return null
}
function findRoot(nodes: KnowledgeSpace[], id: string): KnowledgeSpace | null {
  for (const node of nodes) {
    if (node.id === id || findNode(node.children ?? [], id)) return node
  }
  return null
}
function descendantIds(node: KnowledgeSpace): Set<string> {
  const result = new Set<string>()
  for (const child of node.children ?? []) {
    result.add(child.id)
    for (const id of descendantIds(child)) result.add(id)
  }
  return result
}
</script>

<style scoped>
.space-panel{display:grid;grid-template-rows:auto auto minmax(0,1fr);min-width:0;height:100%;overflow:hidden;border-right:1px solid var(--astra-border);background:#fbfdfc}
.space-panel__header{display:flex;align-items:center;justify-content:space-between;min-height:64px;padding:12px 14px 10px}
.space-panel h3,.space-panel p{margin:0}
.space-panel h3{color:var(--astra-text-primary)}
.space-panel p{margin-top:3px;color:var(--astra-text-muted);font-size:var(--astra-font-helper)}
.space-panel__header>button{display:grid;place-items:center;width:30px;height:30px;padding:0;color:var(--astra-primary-bright);border:1px solid var(--astra-primary-border);border-radius:7px;background:#fff;cursor:pointer}
.space-panel__header>button:hover{background:var(--astra-primary-soft)}
.space-panel__search{width:auto;margin:0 10px 8px}
.space-panel nav{min-height:0;overflow:auto;padding:3px 8px 10px}
.space-all{display:flex;align-items:center;justify-content:space-between;width:100%;min-height:40px;padding:0 10px;color:var(--astra-text-secondary);border:0;border-radius:8px;background:transparent;cursor:pointer}
.space-all:hover{background:#f3f8f5}
.space-all.selected{color:var(--astra-primary-bright);background:var(--astra-primary-soft);box-shadow:inset 3px 0 0 var(--astra-primary);font-weight:var(--astra-weight-bold)}
.space-all span{display:flex;align-items:center;gap:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.space-all strong{color:var(--astra-text-muted);font-size:var(--astra-font-helper);font-weight:var(--astra-weight-bold)}
.space-panel :deep(.el-tree){--el-tree-node-content-height:38px;background:transparent;color:var(--astra-text-secondary)}
.space-panel :deep(.el-tree-node__content){border-radius:8px;padding-right:4px}
.space-panel :deep(.el-tree-node__content:hover){background:#f3f8f5}
.space-panel :deep(.el-tree-node__expand-icon){color:var(--astra-text-muted)}
.space-panel :deep(.el-tree-node.is-current>.el-tree-node__content){color:var(--astra-primary-bright);background:var(--astra-primary-soft);box-shadow:inset 3px 0 0 var(--astra-primary);font-weight:var(--astra-weight-bold)}
.space-node{display:flex;align-items:center;gap:7px;min-width:0;flex:1;padding-right:4px;font-size:var(--astra-font-control)}
.space-node__icon{flex:none}
.space-node__name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.space-node__count{margin-left:auto;flex:none;color:var(--astra-text-muted);font-size:var(--astra-font-helper);font-weight:var(--astra-weight-bold)}
.space-node__menu{display:grid;place-items:center;flex:none;width:26px;height:26px;padding:0;color:var(--astra-text-muted);border:0;border-radius:6px;background:transparent;cursor:pointer}
.space-node__menu:hover{color:var(--astra-primary-bright);background:#fff;box-shadow:0 0 0 1px var(--astra-border)}
</style>

<style>
.space-node__menu-danger{color:var(--astra-danger)}
.space-node__menu-danger:hover{color:var(--astra-danger);background:var(--astra-danger-soft)}
</style>
