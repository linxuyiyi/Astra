<template>
  <section class="knowledge-list">
    <header class="knowledge-list__header">
      <h3 :title="spacePath">{{ spacePath }}</h3>
      <ElButton
        v-if="hasFilters"
        text
        type="primary"
        data-testid="knowledge-clear-filter"
        @click="emit('clear-filter')"
      >清除筛选</ElButton>
    </header>

    <div class="admin-page__toolbar" role="search" aria-label="知识筛选">
      <div class="admin-page__filters">
        <ElSelect
          class="admin-page__select"
          data-testid="knowledge-filter-type"
          :model-value="filters.type"
          aria-label="知识类型"
          placeholder="类型：全部"
          clearable
          @update:model-value="applyFilter('type', $event)"
        >
          <ElOption v-for="item in types" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
        <ElSelect
          class="admin-page__select"
          data-testid="knowledge-filter-status"
          :model-value="filters.status"
          aria-label="知识状态"
          placeholder="状态：全部"
          clearable
          @update:model-value="applyFilter('status', $event)"
        >
          <ElOption v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
        <ElSelect
          class="admin-page__select"
          data-testid="knowledge-filter-sort"
          :model-value="filters.sort"
          aria-label="知识排序"
          @update:model-value="applyFilter('sort', $event)"
        >
          <ElOption value="updated_desc" label="最近更新" />
          <ElOption value="updated_asc" label="最早更新" />
          <ElOption value="references_desc" label="引用最多" />
          <ElOption value="title_asc" label="标题排序" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="keywordDraft"
          class="admin-page__keyword"
          data-testid="knowledge-filter-keyword"
          aria-label="搜索当前空间"
          placeholder="搜索当前空间"
          clearable
          @keydown.enter.prevent="runSearch"
          @clear="runSearch"
        >
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="knowledge-search" @click="runSearch">搜索</ElButton>
      </div>
    </div>

    <div v-if="selectedRows.length" class="knowledge-list__batch" aria-label="批量操作">
      <ElText>已选 {{ selectedRows.length }} 条</ElText>
      <ElSpace :size="8">
        <ElButton v-if="canManage" @click="emitBatch('move')">移动</ElButton>
        <ElButton v-if="canManage" @click="emitBatch('archive')">归档</ElButton>
        <ElButton v-if="canReview" @click="emitBatch('publish')">发布</ElButton>
        <ElButton v-if="canManage" type="danger" @click="emitBatch('delete')">删除</ElButton>
        <ElButton text @click="clearSelection">取消选择</ElButton>
      </ElSpace>
    </div>

    <div class="knowledge-table-wrap">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        class="knowledge-table"
        element-loading-text="正在加载知识"
        :data="page.items"
        row-key="id"
        :max-height="TABLE_MAX_HEIGHT_DENSE"
        @row-click="onRowClick"
        @selection-change="onSelectionChange"
      >
        <ElTableColumn v-if="canManage || canReview" type="selection" width="46" :reserve-selection="true" />
        <ElTableColumn label="标题" min-width="280">
          <template #default="{ row }">
            <div class="knowledge-cell">
              <strong :title="row.title">{{ row.title }}</strong>
              <ElText type="info">{{ metaText(row) }}</ElText>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="审核" width="120">
          <template #default="{ row }"><ElTag :type="statusTag(row.status)">{{ statusLabel(row.status) }}</ElTag></template>
        </ElTableColumn>
        <ElTableColumn label="验证" width="110">
          <template #default="{ row }"><ElTag :type="verificationTag(row.verification_status)" effect="plain">{{ verificationLabel(row.verification_status) }}</ElTag></template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="176" align="center" fixed="right">
          <template #default="{ row }">
            <div class="admin-actions">
              <ElButton link type="primary" @click.stop="openRow(row)">查看</ElButton>
              <ElButton v-if="canManage" link type="primary" @click.stop="editRow(row)">编辑</ElButton>
              <ElDropdown v-if="rowMenu(row).length" trigger="click" @command="(command: string) => onRowCommand(row, command)">
                <ElButton link type="primary">更多</ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem v-for="item in rowMenu(row)" :key="item.command" :command="item.command" :disabled="item.disabled">
                      {{ item.label }}
                    </ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
          </template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="!loading" :image-size="72" :description="hasFilters ? '没有匹配的知识' : '这里还没有知识'">
            <div class="empty-actions">
              <ElButton v-if="!hasFilters && canManage" type="primary" :icon="Plus" @click="emit('create')">新建知识</ElButton>
              <ElButton v-else-if="hasFilters" @click="emit('clear-filter')">清除筛选</ElButton>
            </div>
          </ElEmpty>
        </template>
      </ElTable>
    </div>

    <!-- 分页右对齐，总数由 el-pagination 的 total 展示，避免与目录路径行重复。 -->
    <div class="admin-page__footer">
      <ElPagination
        :current-page="page.page"
        :page-size="page.page_size"
        :page-sizes="[10, 20, 50]"
        :total="page.total"
        layout="total, sizes, prev, pager, next"
        aria-label="知识列表分页"
        data-testid="knowledge-pagination"
        @current-change="emit('page', $event)"
        @size-change="(size: number) => emit('size', size)"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { Plus, Search } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElSpace, ElTable, ElTableColumn, ElTag, ElText, vLoading } from 'element-plus'
import type { TableInstance } from 'element-plus'
import type { KnowledgeListItem, KnowledgePage, KnowledgeStatus, VerificationStatus } from '@/api/types'
import type { RowMenuItem } from '@/components/astra/RowMenu.vue'
import { TABLE_MAX_HEIGHT_DENSE } from '@/utils/table'

// 只保留最常用的查询条件：类型、状态、排序与关键词。
// 来源与标签筛选随「更多筛选」一并取消，filters 里也不再保留这两个维度。
interface ListFilters { type: string; status: string; sort: string }
const props = defineProps<{
  page: KnowledgePage
  filters: ListFilters
  loading: boolean
  canManage: boolean
  canReview: boolean
  spacePath: string
  keyword: string
  filterLabel: string
}>()
const emit = defineEmits<{
  search: [value: string]
  create: []
  open: [id: string]
  edit: [item: KnowledgeListItem]
  move: [item: KnowledgeListItem]
  publish: [item: KnowledgeListItem]
  archive: [item: KnowledgeListItem]
  delete: [item: KnowledgeListItem]
  batch: [ids: string[], action: 'move' | 'archive' | 'publish' | 'delete']
  page: [page: number]
  size: [size: number]
  filter: [key: keyof ListFilters, value: string]
  'clear-filter': []
}>()

const types = [
  { value: 'document', label: '文档' }, { value: 'rule', label: '规范' }, { value: 'guide', label: '指南' },
  { value: 'experience', label: '经验' }, { value: 'faq', label: 'FAQ' }, { value: 'tool', label: '工具' }, { value: 'example', label: '示例' },
]
const statusMeta: Record<KnowledgeStatus, { label: string; tag: 'success' | 'warning' | 'info' | 'danger' }> = {
  published: { label: '已发布', tag: 'success' },
  candidate: { label: '待人工审核', tag: 'warning' },
  draft: { label: '草稿', tag: 'info' },
  archived: { label: '已归档', tag: 'info' },
  rejected: { label: '已驳回', tag: 'danger' },
}
const verificationMeta: Record<VerificationStatus, { label: string; tag: 'success' | 'info' | 'danger' }> = {
  verified: { label: '已验证', tag: 'success' },
  unverified: { label: '未验证', tag: 'info' },
  failed: { label: '验证失败', tag: 'danger' },
}

const keywordDraft = ref(props.keyword)
const selectedRows = ref<KnowledgeListItem[]>([])
const tableRef = ref<TableInstance>()

type Row = Record<string, any>

const statusOptions = computed(() => [
  { value: 'published', label: '已发布' },
  ...(props.canReview ? [{ value: 'candidate', label: '待审核经验' }] : []),
  ...(props.canManage || props.canReview ? [{ value: 'draft', label: '草稿' }] : []),
  ...(props.canManage ? [{ value: 'archived', label: '已归档' }] : []),
  ...(props.canReview ? [{ value: 'rejected', label: '已驳回' }] : []),
])
const hasFilters = computed(() => Boolean(props.keyword || props.filters.type || props.filters.status))

watch(() => props.keyword, (value) => { keywordDraft.value = value })

function runSearch() { emit('search', keywordDraft.value.trim()) }
function applyFilter(key: keyof ListFilters, value: unknown) { emit('filter', key, String(value ?? '')) }
function onRowClick(row: Row, column: unknown) {
  if (column && typeof column === 'object' && (column as { type?: string }).type === 'selection') return
  emit('open', row.id)
}
function onSelectionChange(rows: Row[]) { selectedRows.value = rows as KnowledgeListItem[] }
function clearSelection() { tableRef.value?.clearSelection() }
function emitBatch(action: 'move' | 'archive' | 'publish' | 'delete') { const ids = selectedRows.value.map((item) => item.id); clearSelection(); emit('batch', ids, action) }
function openRow(row: Row) { emit('open', row.id) }
function editRow(row: Row) { emit('edit', row as KnowledgeListItem) }
function moveRow(row: Row) { emit('move', row as KnowledgeListItem) }
function rowMenu(row: Row): RowMenuItem[] {
  const items: RowMenuItem[] = []
  if (props.canManage) items.push({ command: 'move', label: '移动' })
  if (props.canReview && (row.status === 'candidate' || row.status === 'draft')) items.push({ command: 'publish', label: '审核发布' })
  if (props.canManage && row.status !== 'archived') items.push({ command: 'archive', label: '归档' })
  if (props.canManage) items.push({ command: 'delete', label: '删除' })
  return items
}
function onRowCommand(row: Row, command: string) {
  if (command === 'edit') editRow(row)
  else if (command === 'move') moveRow(row)
  else if (command === 'publish') publishRow(row)
  else if (command === 'archive') archiveRow(row)
  else if (command === 'delete') deleteRow(row)
}
function publishRow(row: Row) { emit('publish', row as KnowledgeListItem) }
function archiveRow(row: Row) { emit('archive', row as KnowledgeListItem) }
function deleteRow(row: Row) { emit('delete', row as KnowledgeListItem) }
const typeLabel = (value: string) => types.find((item) => item.value === value)?.label ?? value
const statusTag = (value: KnowledgeStatus) => (statusMeta[value] ?? statusMeta.draft).tag
const statusLabel = (value: KnowledgeStatus) => (statusMeta[value] ?? { label: value, tag: 'info' as const }).label
const verificationTag = (value: VerificationStatus) => (verificationMeta[value] ?? verificationMeta.unverified).tag
const verificationLabel = (value: VerificationStatus) => (verificationMeta[value] ?? { label: value, tag: 'info' as const }).label
const metaText = (row: Row) => [row.id, typeLabel(row.type), row.space_name ?? '未归类', formatDate(row.updated_at)].filter(Boolean).join(' · ')
const formatDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
</script>

<style scoped>
/* 筛选面板、行操作与表格外壳统一走 styles/page-layout.css，
   这里只保留「目录路径行、批量操作条、单元格排版」这些本页独有的部分。 */
.knowledge-list{display:flex;flex-direction:column;gap:16px;min-width:0;background:var(--astra-bg-panel)}
/* 目录路径行只做上下文提示，不再重复展示列表总数 —— 总数只在分页左侧出现一次。 */
.knowledge-list__header{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:40px;padding:0 2px 1px}
.knowledge-list h3{overflow:hidden;margin:0;color:var(--astra-text-primary);font-size:var(--astra-font-label);text-overflow:ellipsis;white-space:nowrap}
.knowledge-list__batch{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 12px;border:1px solid var(--astra-border);border-radius:10px;background:var(--astra-primary-soft)}
.knowledge-table{--el-table-border-color:var(--astra-border)}
.knowledge-table .el-table__row{cursor:pointer}
.knowledge-cell{display:grid;min-width:0;gap:2px}
.knowledge-cell strong{display:-webkit-box;overflow:hidden;color:var(--astra-text-primary);font-size:var(--astra-font-label);line-height:1.45;white-space:normal;-webkit-box-orient:vertical;-webkit-line-clamp:2}
.empty-actions{display:flex;justify-content:center;gap:8px}
</style>
