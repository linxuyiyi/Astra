<template>
  <ElCard class="admin-card digital-library-card" data-testid="digital-library-page">
    <template #header>
      <div class="admin-card__header">
        <h2 class="admin-card__title">数字图书馆</h2>
        <div class="admin-card__actions">
          <ElButton :icon="RefreshCw" :loading="refreshing" data-testid="library-refresh" @click="refreshAll">刷新</ElButton>
          <template v-if="permissions?.can_manage">
            <ElButton :icon="Upload" data-testid="library-import" @click="triggerImport">导入知识</ElButton>
            <ElButton type="primary" :icon="Plus" data-testid="library-create" @click="beginCreate">新建知识</ElButton>
          </template>
        </div>
      </div>
    </template>
    <ElAlert v-if="libraryNotice" class="library-notice" :type="libraryNotice.level" show-icon :closable="false" :title="libraryNotice.title" data-testid="library-service-state">
      <p>{{ libraryNotice.next }}</p>
      <ElButton
        v-if="canManage && libraryNotice.resync"
        text
        type="primary"
        :loading="resyncing"
        :disabled="!serviceStatus?.enabled"
        data-testid="library-service-resync"
        @click="resyncKnowledge"
      >重新同步</ElButton>
      <ElButton v-if="libraryNotice.review" text type="primary" data-testid="library-review-queue" @click="applyStat('candidate')">去审核</ElButton>
    </ElAlert>
    <input ref="importInput" class="hidden-input" data-testid="library-import-input" type="file" accept=".md,text/markdown,text/plain" @change="importMarkdown" />

    <ElMain class="library-body">
      <ElAlert v-if="pageError" class="library-error" type="error" show-icon :closable="false" :title="pageError">
        <ElButton @click="loadAll">重试</ElButton>
      </ElAlert>

      <ElContainer class="library-workspace" direction="horizontal">
        <ElAside class="library-aside">
          <KnowledgeSpacePanel
            :spaces="spaces"
            :selected-id="filters.spaceId"
            :total="stats?.visible_knowledge_total ?? 0"
            :can-manage="Boolean(permissions?.can_manage)"
            @select="selectSpace"
            @create="createSpace"
            @rename="renameSpace"
            @move="moveSpace"
            @delete="deleteSpace"
          />
        </ElAside>
        <ElMain class="library-content">
          <KnowledgeList
            :page="page"
            :filters="filters"
            :loading="listLoading"
            :can-manage="Boolean(permissions?.can_manage)"
            :can-review="Boolean(permissions?.can_review)"
            :space-path="selectedPath"
            :keyword="queryInput"
            :filter-label="activeFilterLabel"
            @search="onListSearch"
            @create="beginCreate"
            @open="openDetail"
            @edit="beginEdit($event.id)"
            @move="openMoveDialog"
            @publish="publishFromList"
            @archive="archiveFromList"
            @delete="deleteFromList"
            @batch="onBatch"
            @page="setPage"
            @size="setPageSize"
            @filter="setFilter"
            @clear-filter="clearFilters"
          />
        </ElMain>
      </ElContainer>

      <KnowledgeUsageSummary :summary="usage" />
    </ElMain>

    <KnowledgeEditorDialog :open="editorOpen" :directories="directoryOptions" :saving="editorSaving" :error="editorError" :editing="editorEditing" :initial-content="editorInitialContent" :initial-title="editorInitialTitle" :initial-space-id="editorInitialSpaceId" @close="editorOpen = false" @save="saveKnowledge" />
    <KnowledgeDetailDialog :open="detailOpen" :detail="detail" :directories="directoryOptions" :can-review="Boolean(permissions?.can_review)" :can-manage="Boolean(permissions?.can_manage)" :working="detailWorking" :error="detailError" :related-tasks="relatedTasks" :related-loading="relatedLoading" :usages="usages" :usages-loading="usagesLoading" @close="closeDetail" @favorite="favorite" @helpful="helpful" @related="loadRelatedTasks" @edit="detail && beginEdit(detail.id)" @publish="publish" @reject="reject" @supplement="supplement" />

    <DirectoryPickerDialog
      :open="Boolean(moveDialog)"
      title="移动知识"
      :subtitle="moveDialog ? `为 ${moveDialog.ids.length} 条知识选择目标目录。` : ''"
      :spaces="spaces"
      :selected-id="moveSpaceId"
      confirm-label="移动"
      :busy="moving"
      @close="moveDialog = null"
      @confirm="submitMove"
    />

    <AstraDialog
      v-if="publishDialog"
      :open="true"
      title="发布知识"
      :subtitle="`为 ${publishDialog.ids.length} 条知识选择目录并发布。`"
      :dismissible="false"
      :busy="working"
      testid="knowledge-publish-dialog"
      card-class="move-knowledge-card"
      width="min(460px,100%)"
      @close="publishDialog = null"
    >
      <form id="knowledge-publish-form" @submit.prevent="submitBatchPublish">
        <label>目标目录<select v-model="publishSpaceId" required><option value="" disabled>选择目录</option><option v-for="item in directoryOptions" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
        <label>验证状态<select v-model="publishVerification"><option value="unverified">未验证</option><option value="verified">已验证</option><option value="failed">验证失败</option></select></label>
      </form>
      <template #footer>
        <ElButton :disabled="working" @click="publishDialog = null">取消</ElButton>
        <ElButton native-type="submit" form="knowledge-publish-form" type="primary" :loading="working" :disabled="!publishSpaceId">发布</ElButton>
      </template>
    </AstraDialog>

    <AstraDialog
      v-if="deleteBlocked"
      :open="true"
      title="无法删除目录"
      :subtitle="`「${deleteBlocked.name}」不是空目录。`"
      dismissible
      testid="knowledge-space-delete-blocked"
      width="min(460px,100%)"
      @close="deleteBlocked = null"
    >
      <div class="delete-blocked">
        <p>该目录下还有 <strong>{{ Array.isArray(deleteBlocked.children) ? deleteBlocked.children.length : 0 }}</strong> 个直接子目录、<strong>{{ deleteBlocked.knowledge_count }}</strong> 条直接知识（子树共 {{ deleteBlocked.subtree_knowledge_count }} 条知识）。</p>
        <p>请先删除或移动这些子目录与知识，待目录为空后再删除本目录。</p>
      </div>
      <template #footer>
        <ElButton type="primary" @click="deleteBlocked = null">知道了</ElButton>
      </template>
    </AstraDialog>
  </ElCard>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Upload } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElAlert, ElAside, ElButton, ElCard, ElContainer, ElMain } from 'element-plus'
import { api } from '@/api/client'
import type { KnowledgeDetail, KnowledgeListItem, KnowledgePage, KnowledgeSpace, KnowledgeUsageRecord, LibraryPermissions, LibraryStats as LibraryStatsData, LibraryUsageSummary, LibraryServiceStatus, RelatedTask } from '@/api/types'
import AstraDialog from '@/components/astra/AstraDialog.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import DirectoryPickerDialog from '@/views/library/DirectoryPickerDialog.vue'
import KnowledgeDetailDialog from '@/views/library/KnowledgeDetailDialog.vue'
import KnowledgeEditorDialog from '@/views/library/KnowledgeEditorDialog.vue'
import KnowledgeList from '@/views/library/KnowledgeList.vue'
import KnowledgeSpacePanel from '@/views/library/KnowledgeSpacePanel.vue'
import KnowledgeUsageSummary from '@/views/library/KnowledgeUsageSummary.vue'

const route = useRoute()
const router = useRouter()
const emptyPage = (): KnowledgePage => ({ items: [], total: 0, page: 1, page_size: 20, pages: 1 })
const stats = ref<LibraryStatsData | null>(null)
const permissions = ref<LibraryPermissions | null>(null)
const spaces = ref<KnowledgeSpace[]>([])
const usage = ref<LibraryUsageSummary | null>(null)
const serviceStatus = ref<LibraryServiceStatus | null>(null)
const serviceError = ref('')
const resyncing = ref(false)
const refreshing = ref(false)
let serviceLoading = false
async function loadServiceStatus() {
  if (serviceLoading) return
  serviceLoading = true
  try { serviceStatus.value = await api.libraryServiceStatus(); serviceError.value = '' }
  catch (error) { serviceError.value = message(error, '知识服务状态加载失败'); serviceStatus.value = null }
  finally {
    serviceLoading = false
  }
}
async function refreshAll() {
  if (refreshing.value) return
  refreshing.value = true
  try { await Promise.all([loadAll(), loadServiceStatus()]) } finally { refreshing.value = false }
}
async function resyncKnowledge() {
  resyncing.value = true
  try {
    const result = await api.libraryResync()
    notify(`已提交 ${result.pending_count} 条同步任务`)
    await loadServiceStatus()
  } catch (error) { serviceError.value = message(error, '重新同步失败') }
  finally { resyncing.value = false }
}
const page = ref<KnowledgePage>(emptyPage())
const filters = reactive({ spaceId: null as string | null, type: '', status: '', sort: 'updated_desc', page: 1, page_size: 20, keyword: '' })
const queryInput = ref('')
const statsActive = ref('all')
const listLoading = ref(false)
const pageError = ref('')
const editorOpen = ref(false)
const editorSaving = ref(false)
const editorError = ref('')
const editorInitialContent = ref('')
const editorInitialTitle = ref('')
const editorInitialSpaceId = ref('')
const editorEditing = ref<KnowledgeDetail | null>(null)
const editorSourceType = ref<'manual' | 'import'>('manual')
const importInput = ref<HTMLInputElement | null>(null)
const detailOpen = ref(false)
const detail = ref<KnowledgeDetail | null>(null)
const detailWorking = ref(false)
const detailError = ref('')
const relatedTasks = ref<RelatedTask[]>([])
const relatedLoading = ref(false)
const usages = ref<KnowledgeUsageRecord[]>([])
const usagesLoading = ref(false)
const moveDialog = ref<{ ids: string[] } | null>(null)
const publishDialog = ref<{ ids: string[] } | null>(null)
const deleteBlocked = ref<KnowledgeSpace | null>(null)
const moveSpaceId = ref('')
const publishSpaceId = ref('')
const publishVerification = ref('verified')
const moving = ref(false)
const working = ref(false)
let searchTimer = 0

const selectedSpace = computed(() => spaces.value.find((space) => space.id === filters.spaceId) ?? findNode(spaces.value, filters.spaceId) ?? null)
const canManage = computed(() => Boolean(permissions.value?.can_manage))
const pendingCount = computed(() => serviceStatus.value?.pending_count ?? 0)
const failedCount = computed(() => serviceStatus.value?.failed_count ?? 0)
const libraryNotice = computed(() => {
  const parts: string[] = []
  const steps: string[] = []
  let level: 'error' | 'warning' = 'warning'
  if (serviceError.value || serviceStatus.value?.healthy === false) {
    level = 'error'
    parts.push(serviceError.value || serviceStatus.value?.message || '知识服务异常，暂时不能确认内容是否最新')
    steps.push('重新同步')
  }
  if (failedCount.value > 0) {
    parts.push(`${failedCount.value} 条同步失败，新内容可能还没进图书馆`)
    steps.push('重新同步')
  } else if (pendingCount.value > 0) {
    parts.push(`${pendingCount.value} 条还在同步，刷新前先别当成已经入库`)
    steps.push('稍后刷新')
  }
  const candidates = stats.value?.candidate_experiences ?? 0
  if (permissions.value?.can_review && candidates > 0) {
    parts.push(`${candidates} 条经验待人工审核，审核和验证是两件独立的事`)
    steps.push('打开待审核列表')
  }
  if (!parts.length) return null
  return {
    level,
    title: parts.join('。'),
    next: `下一步：${[...new Set(steps)].join('，')}。`,
    resync: steps.includes('重新同步'),
    review: steps.includes('打开待审核列表'),
  }
})
const selectedPath = computed(() => {
  if (!filters.spaceId) return '全部知识'
  return findPath(spaces.value, filters.spaceId)?.join(' / ') ?? selectedSpace.value?.name ?? '全部知识'
})
const directoryOptions = computed(() => {
  const result: Array<{ id: string; name: string; level: number }> = []
  const walk = (nodes: KnowledgeSpace[], level: number) => {
    for (const node of nodes) {
      result.push({ id: node.id, name: `${'　'.repeat(level)}${node.name}`, level })
      walk(node.children ?? [], level + 1)
    }
  }
  walk(spaces.value, 0)
  return result
})
const activeFilterLabel = computed(() => {
  if (filters.status === 'candidate') return '候选经验'
  if (filters.type === 'experience' && filters.status === 'published') return '已发布经验'
  if (filters.keyword) return `搜索：${filters.keyword}`
  return ''
})

onMounted(async () => {
  void loadServiceStatus()
  if (route.query.status === 'candidate') {
    filters.type = 'experience'
    filters.status = 'candidate'
    statsActive.value = 'candidate'
  }
  await loadAll()
  const knowledgeId = typeof route.query.knowledge === 'string' ? route.query.knowledge : ''
  if (knowledgeId) await openDetail(knowledgeId, false)
})
onBeforeUnmount(() => { window.clearTimeout(searchTimer) })
watch(queryInput, (value) => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => { filters.keyword = value.trim(); filters.page = 1; statsActive.value = filters.keyword ? 'search' : 'all'; loadList() }, 320)
})
function onListSearch(value: string) {
  if (value === queryInput.value) {
    window.clearTimeout(searchTimer)
    filters.keyword = value; filters.page = 1; statsActive.value = value ? 'search' : 'all'; void loadList()
    return
  }
  queryInput.value = value
}

async function loadAll() {
  pageError.value = ''
  try {
    const [permissionValue, statsValue, spacesValue, usageValue] = await Promise.all([api.libraryPermissions(), api.libraryStats(), api.librarySpaces(), api.libraryUsageSummary(7)])
    permissions.value = permissionValue; stats.value = statsValue; spaces.value = normalizeSpaces(spacesValue); usage.value = usageValue
    await loadList()
  } catch (error) { pageError.value = message(error, '数字图书馆加载失败') }
}
async function loadList() {
  listLoading.value = true
  try {
    const query = new URLSearchParams({ page: String(filters.page), page_size: String(filters.page_size), sort: filters.sort })
    if (filters.spaceId) query.set('space_id', filters.spaceId)
    if (filters.type) query.set('type', filters.type)
    if (filters.status) query.set('status', filters.status)
    if (filters.keyword) query.set('keyword', filters.keyword)
    page.value = await api.libraryKnowledge(`?${query}`)
  } catch (error) { notify(message(error, '知识列表加载失败'), 'error'); page.value = emptyPage() } finally { listLoading.value = false }
}
async function refreshOverview() {
  const [statsValue, spacesValue, usageValue] = await Promise.all([api.libraryStats(), api.librarySpaces(), api.libraryUsageSummary(7)])
  stats.value = statsValue; spaces.value = normalizeSpaces(spacesValue); usage.value = usageValue
}
function applyStat(value: string) {
  if (value === 'spaces') { statsActive.value = value; return }
  filters.spaceId = null; filters.type = ''; filters.status = ''; filters.sort = 'updated_desc'; filters.page = 1; filters.keyword = ''; queryInput.value = ''
  if (value === 'weekly') filters.sort = 'references_desc'
  if (value === 'published') { filters.type = 'experience'; filters.status = 'published' }
  if (value === 'candidate') { filters.type = 'experience'; filters.status = 'candidate' }
  statsActive.value = value; loadList()
}
function selectSpace(id: string | null) { filters.spaceId = id; filters.page = 1; statsActive.value = 'spaces'; loadList() }
function setPage(value: number) { filters.page = value; loadList() }
function setPageSize(value: number) {
  filters.page_size = value
  filters.page = 1
  // 先乐观更新分页控件，等服务端返回再以 page_size 为准，避免切换后回弹。
  page.value = { ...page.value, page_size: value, page: 1 }
  void loadList()
}
function setFilter(key: 'type' | 'status' | 'sort', value: string) { filters[key] = value; filters.page = 1; statsActive.value = value === 'candidate' ? 'candidate' : 'filter'; loadList() }
function clearFilters() { filters.type = ''; filters.status = ''; filters.sort = 'updated_desc'; filters.keyword = ''; filters.page = 1; queryInput.value = ''; statsActive.value = 'all'; loadList() }
function beginCreate() { editorInitialContent.value = ''; editorInitialTitle.value = ''; editorInitialSpaceId.value = filters.spaceId ?? directoryOptions.value[0]?.id ?? ''; editorEditing.value = null; editorSourceType.value = 'manual'; editorError.value = ''; editorOpen.value = true }
async function beginEdit(id: string) {
  editorError.value = ''
  try {
    const latest = await api.libraryKnowledgeDetail(id)
    editorInitialContent.value = ''; editorInitialTitle.value = ''; editorInitialSpaceId.value = ''
    editorEditing.value = latest
    editorSourceType.value = 'manual'
    editorOpen.value = true
  } catch (error) { notify(message(error, '知识加载失败，无法编辑'), 'error') }
}
function publishFromList(item: KnowledgeListItem) { openDetail(item.id) }
function triggerImport() { importInput.value?.click() }
async function importMarkdown(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; editorInitialContent.value = await file.text(); editorInitialTitle.value = file.name.replace(/\.md$/i, ''); editorInitialSpaceId.value = filters.spaceId ?? directoryOptions.value[0]?.id ?? ''; editorEditing.value = null; editorSourceType.value = 'import'; editorError.value = ''; editorOpen.value = true; (event.target as HTMLInputElement).value = '' }
async function saveKnowledge(payload: Record<string, unknown>) {
  editorSaving.value = true; editorError.value = ''
  try {
    const editing = editorEditing.value
    if (editing) {
      const updated = await api.updateLibraryKnowledge(editing.id, payload)
      editorOpen.value = false
      notify(updated.status === 'published' ? '知识修改已保存，已进入同步准备' : '知识修改已保存')
      await Promise.all([refreshOverview(), loadList()])
      if (detailOpen.value && detail.value?.id === updated.id) detail.value = updated
      editorEditing.value = null
      return
    }
    const created = await api.createLibraryKnowledge({ ...payload, source_type: editorSourceType.value })
    editorOpen.value = false; notify('知识草稿已保存')
    await Promise.all([refreshOverview(), loadList()])
    await openDetail(created.id)
  } catch (error) { editorError.value = message(error, '知识保存失败') } finally { editorSaving.value = false }
}
async function createSpace(name: string, parentId: string | null, done: (success: boolean) => void) {
  try {
    const created = await api.createLibrarySpace({ name, parent_id: parentId })
    filters.spaceId = created.id
    filters.page = 1
    notify(parentId ? `已创建子目录「${created.name}」` : `已创建知识空间「${created.name}」`)
    await Promise.all([refreshOverview(), loadList()])
    done(true)
  } catch (error) {
    notify(message(error, '创建失败'), 'error')
    done(false)
  }
}
async function renameSpace(space: KnowledgeSpace, name: string, done: (success: boolean) => void) {
  try {
    await api.updateLibrarySpace(space.id, { name })
    notify(`已重命名为「${name}」`)
    await refreshOverview()
    done(true)
  } catch (error) {
    notify(message(error, '重命名失败'), 'error')
    done(false)
  }
}
async function deleteSpace(space: KnowledgeSpace) {
  const childCount = Array.isArray(space.children) ? space.children.length : 0
  if (childCount > 0 || space.knowledge_count > 0) { deleteBlocked.value = space; return }
  const confirmed = await confirmDialog({ title: '删除目录', message: `确认删除「${space.name}」？删除后无法恢复。`, confirmLabel: '删除目录', danger: true })
  if (!confirmed) return
  try { await api.deleteLibrarySpace(space.id); if (filters.spaceId === space.id) filters.spaceId = null; notify('目录已删除'); await Promise.all([refreshOverview(), loadList()]) } catch (error) { notify(message(error, '删除失败'), 'error') }
}
async function moveSpace(space: KnowledgeSpace, targetParentId: string) {
  try {
    await api.moveLibrarySpace(space.id, targetParentId)
    notify(`已移动「${space.name}」`)
    await refreshOverview()
  } catch (error) { notify(message(error, '移动失败'), 'error') }
}
function openMoveDialog(item: KnowledgeListItem) { moveSpaceId.value = item.space_id ?? ''; moveDialog.value = { ids: [item.id] } }
async function submitMove(targetSpaceId: string) {
  if (!moveDialog.value || !targetSpaceId) return
  moving.value = true
  moveSpaceId.value = targetSpaceId
  try {
    await api.batchMoveLibraryKnowledge(moveDialog.value.ids, targetSpaceId)
    moveDialog.value = null
    notify('知识已移动')
    await Promise.all([refreshOverview(), loadList()])
  } catch (error) { notify(message(error, '移动失败'), 'error') } finally { moving.value = false }
}
async function archiveFromList(item: KnowledgeListItem) { try { await api.archiveLibraryKnowledge(item.id); notify('已归档'); await Promise.all([refreshOverview(), loadList()]) } catch (error) { notify(message(error, '归档失败'), 'error') } }
async function deleteFromList(item: KnowledgeListItem) {
  const confirmed = await confirmDialog({ title: '删除知识', message: `确认删除「${item.title}」？删除后无法恢复。`, confirmLabel: '删除知识', danger: true })
  if (!confirmed) return
  try { await api.deleteLibraryKnowledge(item.id); notify('已删除'); await Promise.all([refreshOverview(), loadList()]) } catch (error) { notify(message(error, '删除失败'), 'error') }
}
function onBatch(ids: string[], action: 'move' | 'archive' | 'publish' | 'delete') {
  if (action === 'move') { moveSpaceId.value = ''; moveDialog.value = { ids }; return }
  if (action === 'publish') { publishSpaceId.value = ''; publishVerification.value = 'verified'; publishDialog.value = { ids }; return }
  if (action === 'archive') { void api.batchArchiveLibraryKnowledge(ids).then(() => { notify('已批量归档'); return Promise.all([refreshOverview(), loadList()]) }).catch((error) => { notify(message(error, '批量归档失败'), 'error') }); return }
  if (action === 'delete') {
    void confirmDialog({ title: '批量删除知识', message: `确认删除选中的 ${ids.length} 条知识？删除后无法恢复。`, confirmLabel: '批量删除', danger: true }).then((confirmed) => {
      if (!confirmed) return
      return api.batchDeleteLibraryKnowledge(ids).then(() => { notify('已批量删除'); return Promise.all([refreshOverview(), loadList()]) }).catch((error) => { notify(message(error, '批量删除失败'), 'error') })
    })
  }
}
async function submitBatchPublish() {
  if (!publishDialog.value || !publishSpaceId.value) return
  working.value = true
  try { await api.batchPublishLibraryKnowledge(publishDialog.value.ids, publishSpaceId.value, publishVerification.value); publishDialog.value = null; notify('候选经验已发布'); await Promise.all([refreshOverview(), loadList()]) } catch (error) { notify(message(error, '发布失败'), 'error') } finally { working.value = false }
}
async function openDetail(id: string, syncUrl = true) {
  detailOpen.value = true; detail.value = null; detailError.value = ''; relatedTasks.value = []; usages.value = []; usagesLoading.value = true
  try {
    detail.value = await api.libraryKnowledgeDetail(id)
    if (syncUrl) await router.replace({ query: { ...route.query, knowledge: id } })
    try { usages.value = await api.libraryKnowledgeUsages(id) } catch { usages.value = [] }
  } catch (error) { detailError.value = message(error, '知识详情加载失败') } finally { usagesLoading.value = false }
}
async function closeDetail() { detailOpen.value = false; detail.value = null; const query = { ...route.query }; delete query.knowledge; await router.replace({ query }) }
async function favorite() { if (!detail.value) return; try { const result = await api.favoriteLibraryKnowledge(detail.value.id); detail.value.is_favorite = result.favorite } catch (error) { detailError.value = message(error, '收藏操作失败') } }
async function helpful() { if (!detail.value) return; try { const result = await api.helpfulLibraryKnowledge(detail.value.id); detail.value.helpful_count = result.helpful_count; detail.value.user_helpful = true; notify(result.created ? '已记录“有帮助”反馈' : '你已反馈过这条知识') } catch (error) { detailError.value = message(error, '反馈提交失败') } }
async function loadRelatedTasks() { if (!detail.value || relatedLoading.value) return; relatedLoading.value = true; try { relatedTasks.value = await api.libraryRelatedTasks(detail.value.id) } catch (error) { detailError.value = message(error, '关联任务加载失败') } finally { relatedLoading.value = false } }
async function publish(payload: Record<string, unknown>) { if (!detail.value) return; await reviewAction(async () => api.publishLibraryKnowledge(detail.value!.id, payload), '候选经验已发布，数字员工现可立即检索') }
async function reject(reason: string) { if (!detail.value) return; await reviewAction(async () => api.rejectLibraryKnowledge(detail.value!.id, reason), '候选经验已驳回') }
async function supplement(reason: string) { if (!detail.value) return; await reviewAction(async () => api.supplementLibraryKnowledge(detail.value!.id, reason), '已记录退回补充要求') }
async function reviewAction(action: () => Promise<KnowledgeDetail>, success: string) { detailWorking.value = true; detailError.value = ''; try { detail.value = await action(); notify(success); await Promise.all([refreshOverview(), loadList()]) } catch (error) { const text = message(error, '审核操作失败'); detailError.value = text; notify(text, 'error') } finally { detailWorking.value = false } }
function message(error: unknown, fallback: string) { return error instanceof Error ? error.message : fallback }
function findNode(nodes: KnowledgeSpace[], id: string | null): KnowledgeSpace | null {
  if (!id) return null
  for (const node of nodes) { if (node.id === id) return node; const found = findNode(node.children ?? [], id); if (found) return found }
  return null
}
function findPath(nodes: KnowledgeSpace[], id: string, parents: string[] = []): string[] | null {
  for (const node of nodes) {
    const path = [...parents, node.name]
    if (node.id === id) return path
    const found = findPath(node.children ?? [], id, path)
    if (found) return found
  }
  return null
}
function normalizeSpaces(nodes: KnowledgeSpace[]): KnowledgeSpace[] {
  return nodes.map((node) => {
    const children = normalizeSpaces(Array.isArray(node.children) ? node.children : [])
    const directCount = Number.isFinite(node.knowledge_count) ? node.knowledge_count : 0
    const subtreeCount = Number.isFinite(node.subtree_knowledge_count)
      ? node.subtree_knowledge_count
      : directCount + children.reduce((total, child) => total + child.subtree_knowledge_count, 0)
    return { ...node, knowledge_count: directCount, subtree_knowledge_count: subtreeCount, children }
  })
}
</script>

<style scoped>
/* 标题区、筛选面板、表格面板与页脚统一走 styles/page-layout.css。
   本页只保留「左目录树 + 右知识表格」的双栏骨架与页面独有的排版。 */
.hidden-input{display:none}
.library-body{--el-main-padding:0;display:flex;flex-direction:column;gap:16px;min-width:0;overflow:visible}
.library-error{flex:none}
.library-service-reason{overflow:hidden;max-width:340px;color:var(--astra-danger);text-overflow:ellipsis;white-space:nowrap}
.library-workspace{display:flex;min-height:360px;border:1px solid var(--astra-border);border-radius:12px;background:var(--astra-bg-panel);box-shadow:var(--astra-shadow-panel)}
.library-aside{--el-aside-width:300px;overflow:visible}
.library-content{--el-main-padding:0;min-width:0;overflow:visible}
/* 发布弹窗用原生 select：标签行高与控件高度都对齐全局 32px 基准，
   与 ElForm（label-position="top"）的单列表单保持同一节奏。 */
.move-knowledge-card label{display:grid;grid-template-rows:var(--astra-control-height) auto;gap:2px;margin-top:14px;color:var(--astra-text-secondary);font-size:var(--astra-font-body);line-height:var(--astra-control-height)}
.move-knowledge-card select{height:var(--astra-control-height);padding:0 9px;color:var(--astra-text-primary);border:1px solid var(--astra-border);border-radius:8px;background:#fff;font:inherit}
.delete-blocked{display:grid;gap:8px}.delete-blocked p{margin:0;color:var(--astra-text-secondary);line-height:1.6}.delete-blocked p strong{color:var(--astra-text-primary)}
@media(max-width:1100px){.library-aside{--el-aside-width:270px}}
@media(max-width:780px){.library-workspace{min-height:420px}.library-aside{flex:none;height:300px;--el-aside-width:auto}.library-content{min-height:420px}.library-service-reason{max-width:100%}}
</style>
