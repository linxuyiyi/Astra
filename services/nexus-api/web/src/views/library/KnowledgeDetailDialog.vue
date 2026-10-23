<template>
  <ElDrawer
    v-if="open"
    class="knowledge-detail-drawer"
    data-testid="knowledge-detail-dialog"
    :model-value="true"
    size="min(980px, 100%)"
    :before-close="() => requestClose()"
  >
    <template #header>
      <div class="detail-header">
        <div class="detail-header__text">
          <div class="detail-header__badges">
            <ElTag v-if="detail">{{ typeLabel(detail.type) }}</ElTag>
            <ElTag v-for="tag in detail?.tags ?? []" :key="tag" type="info" effect="plain">{{ tag }}</ElTag>
          </div>
          <h2 :id="detail ? `knowledge-${detail.id}` : 'knowledge-detail-title'">{{ detail?.title ?? (error ? '知识详情加载失败' : '正在加载知识详情') }}</h2>
          <div v-if="detail" class="detail-header__meta">
            <ElTag :type="statusTagType(detail.status)">审核状态 · {{ statusLabel(detail.status) }}</ElTag>
            <ElTag :type="verificationTagType(detail.verification_status)">证据状态 · {{ verificationLabel(detail.verification_status) }}</ElTag>
            <a v-if="detail.source_task_id" :href="`/tasks/${detail.source_task_id}`" @click="onTaskLinkClick($event, `/tasks/${detail.source_task_id}`)">来源任务：{{ detail.source_task_id }}</a>
            <ElText type="info">更新 {{ formatDate(detail.updated_at) }}</ElText>
            <ElText type="info">引用 {{ detail.reference_count }}</ElText>
          </div>
        </div>
        <ElButton class="detail-header__close" text :icon="X" aria-label="关闭知识详情" @click="requestClose" />
      </div>
    </template>

    <div class="detail-scroll">
      <ElAlert v-if="error" class="detail-error" type="error" show-icon :closable="false" :title="error" />
      <ElSkeleton v-if="!detail && !error" class="detail-loading" animated :rows="6" />

      <template v-else-if="detail">
        <ElDescriptions class="detail-section" :column="3" border>
          <ElDescriptionsItem label="类型">{{ typeLabel(detail.type) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="所属目录">{{ spaceName }}</ElDescriptionsItem>
          <ElDescriptionsItem label="来源">{{ sourceTypeLabel(detail.source_type) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="来源 Worker">{{ detail.source_employee_name || detail.source_employee_id || '—' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="执行会话">{{ detail.source_session_id || '—' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="验证时间">{{ detail.verified_at ? formatDate(detail.verified_at) : '—' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="有帮助">{{ detail.helpful_count }}</ElDescriptionsItem>
          <ElDescriptionsItem label="更新时间">{{ formatDate(detail.updated_at) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="发布时间">{{ detail.published_at ? formatDate(detail.published_at) : '尚未发布' }}</ElDescriptionsItem>
        </ElDescriptions>

        <section v-if="detail.status === 'candidate'" class="detail-section" aria-label="候选经验来源与证据">
          <h3>来源证据</h3>
          <ElAlert v-if="detail.review_note" class="candidate-review-note" type="warning" show-icon :closable="false">
            <template #title>审核意见</template>
            <p class="candidate-review-note__body">{{ detail.review_note }}</p>
          </ElAlert>
          <ElDescriptions v-if="candidateFacts.length" class="origin-facts" :column="3" border>
            <ElDescriptionsItem v-for="fact in candidateFacts" :key="fact.label" :label="fact.label">{{ fact.value }}</ElDescriptionsItem>
          </ElDescriptions>
          <ElDescriptions v-if="experienceFields.length" class="origin-facts" :column="2" border>
            <ElDescriptionsItem v-for="field in experienceFields" :key="field.label" :label="field.label">{{ field.value }}</ElDescriptionsItem>
          </ElDescriptions>
          <p class="origin-facts__title">相关日志 / 会话 / Task 证据</p>
          <ElTable v-if="detail.evidence.length" :data="detail.evidence" border>
            <ElTableColumn label="证据类型" prop="evidence_type" width="110" />
            <ElTableColumn label="内容" min-width="220">
              <template #default="{ row }">{{ row.content || row.uri || '已关联结构化证据' }}</template>
            </ElTableColumn>
            <ElTableColumn label="Task" width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.task_id || '—' }}</template>
            </ElTableColumn>
            <ElTableColumn label="会话" width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.native_session_id || row.source_session_id || '—' }}</template>
            </ElTableColumn>
            <ElTableColumn label="结果" width="96">
              <template #default="{ row }">
                <ElTag :type="row.result ? (row.result.includes('fail') || row.result.includes('error') ? 'danger' : 'success') : 'info'">{{ row.result || '未标记结果' }}</ElTag>
              </template>
            </ElTableColumn>
            <ElTableColumn label="链接" width="70">
              <template #default="{ row }">
                <ElLink v-if="safeExternalUrl(row.uri)" :href="safeExternalUrl(row.uri)" target="_blank" rel="noopener noreferrer" type="primary">打开</ElLink>
                <span v-else>—</span>
              </template>
            </ElTableColumn>
          </ElTable>
          <ElText v-else class="detail-loading-text" type="info">暂无额外证据</ElText>
        </section>

        <section class="detail-section">
          <div class="detail-section__head">
            <h3>{{ reviewEditing ? '发布前编辑' : '知识正文' }}</h3>
            <ElTag v-if="reviewDirty" type="warning" effect="light">有未保存修改</ElTag>
          </div>
          <ElForm v-if="reviewEditing" class="review-form" :model="review" label-position="top">
            <ElFormItem required>
              <template #label>目标目录</template>
              <ElSelect v-model="review.space_id" placeholder="请选择">
                <ElOption v-for="dir in directories" :key="dir.id" :value="dir.id" :label="dir.name" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="验证状态">
              <ElSelect v-model="review.verification_status">
                <ElOption value="unverified" label="未验证" />
                <ElOption value="verified" label="已验证" />
                <ElOption value="failed" label="验证失败" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="标题" required>
              <ElInput v-model.trim="review.title" maxlength="240" show-word-limit />
            </ElFormItem>
            <ElFormItem label="标签">
              <ElInput v-model="review.tags" placeholder="用逗号分隔" />
            </ElFormItem>
            <ElFormItem label="摘要">
              <ElInput v-model="review.summary" type="textarea" :rows="2" maxlength="4000" show-word-limit />
            </ElFormItem>
            <ElFormItem label="Markdown 正文" required>
              <ElInput v-model="review.content_md" class="review-form__content" type="textarea" :rows="16" resize="vertical" />
            </ElFormItem>
          </ElForm>
          <div v-else class="markdown-body" v-html="safeMarkdown" />
        </section>

        <section v-if="reviewActionMode && detail.status === 'candidate'" ref="reviewRequestPanel" class="review-request-panel">
          <div>
            <strong>{{ reviewActionMode === 'supplement' ? '退回补充' : '驳回候选经验' }}</strong>
          </div>
          <ElInput ref="reviewReasonInput" v-model.trim="review.reason" type="textarea" :rows="3" :disabled="working" :placeholder="reviewActionMode === 'supplement' ? '补充要求' : '请输入驳回原因'" />
          <ElSpace class="review-request-panel__actions" :size="8">
            <ElButton :disabled="working" @click="reviewActionMode = null">取消</ElButton>
            <ElButton :type="reviewActionMode === 'reject' ? 'danger' : 'primary'" :loading="working" :disabled="review.reason.length < 2" @click="confirmReviewAction">
              {{ reviewActionMode === 'supplement' ? '确认退回补充' : '确认驳回' }}
            </ElButton>
          </ElSpace>
        </section>

        <section v-if="detail.sources.length" class="detail-section">
          <h3>资料来源</h3>
          <ElTable :data="detail.sources" border>
            <ElTableColumn label="来源" min-width="200">
              <template #default="{ row }">{{ row.source_name || row.source_id || row.source_type }}</template>
            </ElTableColumn>
            <ElTableColumn label="类型" width="150">
              <template #default="{ row }">{{ row.source_type }}<template v-if="row.version"> · {{ row.version }}</template></template>
            </ElTableColumn>
            <ElTableColumn label="链接" width="90">
              <template #default="{ row }">
                <ElLink v-if="safeExternalUrl(row.source_uri)" :href="safeExternalUrl(row.source_uri)" target="_blank" rel="noopener noreferrer" type="primary">查看来源</ElLink>
                <span v-else>—</span>
              </template>
            </ElTableColumn>
          </ElTable>
        </section>

        <section class="detail-section">
          <h3>最近引用</h3>
          <ElTable v-if="usages.length" :data="usages" border>
            <ElTableColumn label="任务" min-width="220">
              <template #default="{ row }">
                <a v-if="row.task_id" :href="`/tasks/${row.task_id}`" @click="onTaskLinkClick($event, `/tasks/${row.task_id}`)">{{ row.task_title || row.task_id }}</a>
                <span v-else>{{ row.task_title || '引用' }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="执行 Worker" width="140" show-overflow-tooltip>
              <template #default="{ row }">{{ row.employee_name || row.employee_id || 'Worker' }}</template>
            </ElTableColumn>
            <ElTableColumn label="动作" prop="action" width="110" />
            <ElTableColumn label="引用时间" width="150">
              <template #default="{ row }">{{ formatDate(row.used_at) }}</template>
            </ElTableColumn>
          </ElTable>
          <ElText v-else-if="usagesLoading" class="detail-loading-text">正在加载引用记录……</ElText>
          <ElText v-else class="detail-loading-text" type="info">暂无知识引用记录。</ElText>
          <ElButton v-if="usages.length" text type="primary" @click="openRelated">查看全部关联任务</ElButton>
        </section>

        <section v-if="relatedOpen" class="detail-section">
          <div class="detail-section__head">
            <div>
              <h3>关联任务</h3>
            </div>
            <ElButton text :icon="X" aria-label="关闭关联任务" @click="relatedOpen = false" />
          </div>
          <ElTable v-if="relatedTasks.length || !relatedLoading" :data="relatedTasks" border>
            <ElTableColumn label="任务" min-width="220">
              <template #default="{ row }">
                <a :href="`/tasks/${row.id}`" @click="onTaskLinkClick($event, `/tasks/${row.id}`)">{{ row.title }}</a>
                <ElText type="info"> · {{ row.id }}</ElText>
              </template>
            </ElTableColumn>
            <ElTableColumn label="关系" prop="relation" width="110" />
            <ElTableColumn label="状态" prop="status" width="110" />
            <ElTableColumn label="最近引用" width="150">
              <template #default="{ row }">{{ row.last_used_at ? formatDate(row.last_used_at) : '—' }}</template>
            </ElTableColumn>
            <template #empty>暂无关联任务记录。</template>
          </ElTable>
          <ElText v-else class="detail-loading-text">正在加载关联任务……</ElText>
        </section>
      </template>
    </div>

    <template v-if="detail" #footer>
      <div class="detail-footer">
        <ElSpace class="detail-footer__actions" :size="8">
          <ElButton :type="detail.is_favorite ? 'warning' : 'default'" :plain="!detail.is_favorite" :icon="Star" :disabled="working" data-testid="knowledge-detail-favorite" @click="emit('favorite')">{{ detail.is_favorite ? '已收藏' : '收藏' }}</ElButton>
          <ElButton :type="detail.user_helpful ? 'primary' : 'default'" :plain="!detail.user_helpful" :icon="ThumbsUp" :disabled="working" data-testid="knowledge-detail-helpful" @click="emit('helpful')">有帮助 ({{ detail.helpful_count }})</ElButton>
          <ElButton :icon="Share2" @click="share">分享</ElButton>
          <ElButton :icon="Link2" @click="copyLink">{{ copied ? '已复制' : '复制链接' }}</ElButton>
          <ElButton :icon="ListTree" @click="openRelated">查看关联任务</ElButton>
          <ElButton v-if="canManage" :icon="Pencil" data-testid="knowledge-detail-edit" @click="emit('edit')">编辑</ElButton>
        </ElSpace>
        <ElSpace v-if="canReview && ['candidate', 'draft'].includes(detail.status)" class="detail-footer__review" :size="8">
          <ElButton :icon="reviewEditing ? Eye : ClipboardEdit" :disabled="working" data-testid="knowledge-detail-review-edit" @click="toggleReviewEditing">{{ reviewEditing ? '预览正文' : '编辑' }}</ElButton>
          <template v-if="detail.status === 'candidate'">
            <ElButton type="danger" plain :disabled="working" @click="startReviewAction('reject')">驳回</ElButton>
            <ElButton :disabled="working" @click="startReviewAction('supplement')">退回补充</ElButton>
          </template>
          <ElButton type="primary" :loading="working" :disabled="!canPublish" data-testid="knowledge-detail-publish" @click="publish">发布</ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { ClipboardEdit, Eye, Link2, ListTree, Pencil, Share2, Star, ThumbsUp, X } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElAlert, ElButton, ElDescriptions, ElDescriptionsItem, ElDrawer, ElForm, ElFormItem, ElInput, ElLink, ElOption, ElSelect, ElSkeleton, ElSpace, ElTable, ElTableColumn, ElTag, ElText } from 'element-plus'
import type { InputInstance } from 'element-plus'
import type { KnowledgeDetail, KnowledgeUsageRecord, RelatedTask } from '@/api/types'
import { confirmDialog } from '@/composables/useConfirm'

const props = defineProps<{ open: boolean; detail: KnowledgeDetail | null; directories: Array<{ id: string; name: string; level: number }>; canReview: boolean; canManage: boolean; working: boolean; error: string; relatedTasks: RelatedTask[]; relatedLoading: boolean; usages: KnowledgeUsageRecord[]; usagesLoading: boolean }>()
const emit = defineEmits<{ close: []; favorite: []; helpful: []; related: []; edit: []; publish: [payload: Record<string, unknown>]; reject: [reason: string]; supplement: [reason: string] }>()

const router = useRouter()
const reviewEditing = ref(false)
const relatedOpen = ref(false)
const copied = ref(false)
const reviewActionMode = ref<'supplement' | 'reject' | null>(null)
const reviewRequestPanel = ref<HTMLElement | null>(null)
const reviewReasonInput = ref<InputInstance | null>(null)
const review = reactive({ space_id: '', title: '', summary: '', tags: '', content_md: '', verification_status: 'unverified', reason: '' })
let reviewSnapshot = ''
let lastDetailId: string | null | undefined
let copyTimer = 0
marked.setOptions({ gfm: true, breaks: true })

const editFields = () => JSON.stringify([review.space_id, review.title, review.summary, review.tags, review.content_md, review.verification_status])
const reviewDirty = computed(() => reviewEditing.value && Boolean(props.detail) && editFields() !== reviewSnapshot)

function syncReviewFromDetail() {
  const detail = props.detail
  if (!detail) return
  Object.assign(review, {
    space_id: detail.space_id || props.directories[0]?.id || '',
    title: detail.title,
    summary: detail.summary,
    tags: detail.tags.join(', '),
    content_md: detail.content_md,
    verification_status: detail.verification_status,
    reason: detail.review_note || '',
  })
  reviewSnapshot = editFields()
  if (!['candidate', 'draft'].includes(detail.status)) reviewEditing.value = false
}

watch(
  () => props.detail && [props.detail.id, props.detail.status, props.detail.updated_at, props.detail.verification_status].join('|'),
  () => {
    const detail = props.detail
    if (!detail) return
    if (detail.id !== lastDetailId) {
      lastDetailId = detail.id
      reviewEditing.value = false
      relatedOpen.value = false
      copied.value = false
      reviewActionMode.value = null
    }
    syncReviewFromDetail()
  },
  { immediate: true },
)

onBeforeUnmount(() => window.clearTimeout(copyTimer))

const safeMarkdown = computed(() => DOMPurify.sanitize(marked.parse(props.detail?.content_md || '', { async: false }) as string, { USE_PROFILES: { html: true } }))
const spaceName = computed(() => {
  const detail = props.detail
  if (!detail) return '—'
  if (detail.space_name) return detail.space_name
  return props.directories.find((dir) => dir.id === detail.space_id)?.name.trim() ?? '未归类'
})
const typeLabel = (value: string) => ({ document: '文档', rule: '规范', guide: '指南', experience: '经验', faq: 'FAQ', tool: '工具', example: '示例' }[value] ?? value)
const statusLabel = (value: string) => ({ published: '已发布', candidate: '待人工审核', draft: '草稿', archived: '已归档', rejected: '已驳回' }[value] ?? value)
const statusTagType = (value: string) => (({ published: 'success', candidate: 'warning', draft: 'info', archived: 'info', rejected: 'danger' }[value] ?? 'info') as 'success' | 'warning' | 'info' | 'danger')
const verificationLabel = (value: string) => ({ verified: '已验证', unverified: '未验证', failed: '验证失败' }[value] ?? value)
const verificationTagType = (value: string) => (({ verified: 'success', failed: 'danger' }[value] ?? 'info') as 'success' | 'danger' | 'info')
const sourceTypeLabel = (value: string | null) => ({ task: '任务沉淀', manual: '人工创建', import: '文档导入', system: '系统同步' }[value ?? ''] ?? '人工创建')
const formatDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
function experienceValue(keys: string[]) { for (const key of keys) { const value = props.detail?.experience_data[key]; if (value !== undefined && value !== null && value !== '') return typeof value === 'boolean' ? value ? '通过' : '未通过' : String(value) } return '—' }
function safeExternalUrl(value: string | null | undefined) {
  if (!value) return ''
  try {
    const url = new URL(value, window.location.origin)
    return ['http:', 'https:'].includes(url.protocol) ? value : ''
  } catch {
    return ''
  }
}
const candidateFacts = computed(() => {
  const detail = props.detail
  if (!detail) return []
  const facts = [
    { label: '来源 Worker', value: detail.source_employee_name || detail.source_employee_id || '' },
    { label: '来源任务', value: detail.source_task_title || detail.source_task_id || '' },
    { label: '执行会话', value: detail.source_session_id || '' },
    { label: '证据状态', value: detail.verified_at ? '来源会话已验证' : verificationLabel(detail.verification_status) },
    { label: '执行时间', value: experienceValue(['task_executed_at', 'executed_at']) },
    { label: '处理前', value: experienceValue(['before_status', 'status_before', 'problem_symptom']) },
    { label: '处理后', value: experienceValue(['after_status', 'status_after', 'retry_result', 'run_status']) },
    { label: '验证次数', value: experienceValue(['verification_count', 'retry_count']) },
    { label: '验证结果', value: experienceValue(['verification_passed', 'verification_result']) },
  ]
  return facts.filter((fact) => fact.value && fact.value !== '—')
})
const experienceFields = computed(() => [
  { label: '经验类型', value: experienceValue(['kind', 'experience_type']) },
  { label: '改进对象', value: experienceValue(['target_component']) },
  { label: '建议改动', value: experienceValue(['proposed_change']) },
  { label: '证据等级', value: experienceValue(['evaluation_level']) },
  { label: '问题现象', value: experienceValue(['problem_symptom', 'problem', 'symptom']) },
  { label: '原始异常 / 错误信息', value: experienceValue(['original_error', 'error', 'exception']) },
  { label: 'Agent 采取的处理方式', value: experienceValue(['handling', 'resolution', 'solution']) },
  { label: '修改内容摘要', value: experienceValue(['change_summary', 'changes', 'modification_summary']) },
  { label: '重试与验证结果', value: experienceValue(['retry_result', 'verification_result', 'result']) },
].filter((field) => field.value !== '—'))

async function discardUnsavedReview(actionLabel: string, confirmLabel: string): Promise<boolean> {
  if (!reviewDirty.value) return true
  return confirmDialog({
    title: '放弃未保存的审核补充编辑？',
    message: `发布前编辑的内容尚未提交，${actionLabel}后这些修改将丢失。`,
    confirmLabel,
    danger: true,
  })
}

async function requestClose() {
  if (props.working) return
  if (!(await discardUnsavedReview('关闭详情', '放弃并关闭'))) return
  emit('close')
}

async function toggleReviewEditing() {
  if (!reviewEditing.value) { reviewEditing.value = true; return }
  if (!(await discardUnsavedReview('切换预览', '放弃修改'))) return
  syncReviewFromDetail()
  reviewEditing.value = false
}

async function startReviewAction(mode: 'supplement' | 'reject') {
  if (!(await discardUnsavedReview('该操作不会带上正文编辑', '放弃并继续'))) return
  reviewActionMode.value = mode
  await nextTick()
  reviewRequestPanel.value?.scrollIntoView({ block: 'nearest' })
  reviewReasonInput.value?.textarea?.focus({ preventScroll: true })
}

function confirmReviewAction() {
  if (!reviewActionMode.value || review.reason.length < 2) return
  if (reviewActionMode.value === 'supplement') emit('supplement', review.reason)
  else emit('reject', review.reason)
  reviewActionMode.value = null
}

const canPublish = computed(() => Boolean(props.detail && review.space_id && review.title && review.content_md))
function publish() {
  if (!canPublish.value) return
  emit('publish', { space_id: review.space_id, title: review.title, summary: review.summary, tags: review.tags.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean), content_md: review.content_md, verification_status: review.verification_status })
}

function knowledgeUrl() { const url = new URL(window.location.href); if (props.detail) url.searchParams.set('knowledge', props.detail.id); return url.toString() }
async function copyLink() {
  try { await navigator.clipboard.writeText(knowledgeUrl()); copied.value = true; window.clearTimeout(copyTimer); copyTimer = window.setTimeout(() => { copied.value = false }, 1600) } catch { copied.value = false }
}
async function share() { try { if (navigator.share) await navigator.share({ title: props.detail?.title, url: knowledgeUrl() }); else await copyLink() } catch { /* 用户取消分享 */ } }
function openRelated() { relatedOpen.value = true; emit('related') }
async function onTaskLinkClick(event: MouseEvent, to: string) {
  event.preventDefault()
  if (!(await discardUnsavedReview('离开详情', '放弃并离开'))) return
  emit('close')
  await router.push(to)
}
</script>

<style scoped>
.detail-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 4px 20px 20px; scrollbar-gutter: stable; }
.detail-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; width: 100%; min-width: 0; }
.detail-header__text { min-width: 0; }
.detail-header__badges { display: flex; flex-wrap: wrap; gap: 6px; }
.detail-header h2 { margin: 8px 0 6px; color: var(--astra-text-primary); font-size: var(--astra-font-title); line-height: 1.3; }
.detail-header__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; color: var(--astra-text-muted); font-size: var(--astra-font-body); }
.detail-header__meta a { color: var(--astra-primary-bright); }
.detail-header__close { flex: none; }
.detail-section { margin-top: 18px; }
.detail-section h3 { margin: 0 0 6px; color: var(--astra-text-primary); font-size: var(--astra-font-section); line-height: 1.4; }
.detail-section__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.origin-facts { margin-top: 12px; }
.origin-facts__title { margin: 14px 0 8px; color: var(--astra-text-secondary); font-size: var(--astra-font-body); font-weight: 700; }
.candidate-review-note { margin-top: 10px; }
.candidate-review-note__body { margin: 2px 0 0; }
.detail-loading-text { display: block; margin-top: 10px; color: var(--astra-text-muted); }
.detail-section a { color: var(--astra-primary-bright); }
.review-form { display: grid; gap: 2px; margin-top: 8px; }
.review-form__content :deep(.el-textarea__inner) { font-family: var(--astra-font-family); font-size: var(--astra-font-body); line-height: 1.55; }
.review-request-panel { display: grid; gap: 10px; margin-top: 18px; padding: 14px; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-panel, #fbfdfc); }
.review-request-panel strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.review-request-panel p { margin: 3px 0 0; color: var(--astra-text-muted); font-size: var(--astra-font-body); }
.review-request-panel__actions { justify-content: flex-end; }
.detail-loading { margin-top: 12px; }
.detail-footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 10px; }
.markdown-body { margin-top: 10px; color: var(--astra-text-secondary); font-size: var(--astra-font-size-15); line-height: 1.78; }
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) { color: var(--astra-text-primary); line-height: 1.35; }
.markdown-body :deep(h1) { margin: 0 0 18px; font-size: var(--astra-font-title); }
.markdown-body :deep(h2) { margin: 25px 0 12px; padding-bottom: 7px; border-bottom: 1px solid var(--astra-border); font-size: var(--astra-font-size-20); }
.markdown-body :deep(h3) { margin: 19px 0 9px; font-size: var(--astra-font-section); }
.markdown-body :deep(p) { margin: 9px 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 24px; }
.markdown-body :deep(blockquote) { margin: 14px 0; padding: 10px 14px; color: var(--astra-text-secondary); border-left: 3px solid var(--astra-primary); background: var(--astra-primary-soft); }
.markdown-body :deep(code) { padding: 2px 5px; color: #b45140; border-radius: 5px; background: #f4efec; font-family: var(--astra-font-family); font-size: .9em; }
.markdown-body :deep(pre) { overflow: auto; padding: 14px; border: 1px solid var(--astra-border); border-radius: 9px; background: #15231f; }
.markdown-body :deep(pre code) { padding: 0; color: #dcebe5; background: transparent; }
.markdown-body :deep(table) { display: block; max-width: 100%; overflow: auto; border-collapse: collapse; }
.markdown-body :deep(th), .markdown-body :deep(td) { padding: 8px 10px; border: 1px solid var(--astra-border); }
.markdown-body :deep(th) { background: #f3f8f5; }
.markdown-body :deep(img) { max-width: 100%; height: auto; border-radius: 8px; }
.markdown-body :deep(a) { color: var(--astra-primary-bright); text-decoration: underline; }
.markdown-body :deep(hr) { margin: 24px 0; border: 0; border-top: 1px solid var(--astra-border); }
@media (max-width: 720px) {
  .detail-footer { flex-direction: column; align-items: flex-start; }
}
</style>

<style>
.knowledge-detail-drawer .el-drawer__header { margin-bottom: 0; padding: 16px 20px 14px; border-bottom: 1px solid var(--astra-border); }
.knowledge-detail-drawer .el-drawer__body { display: flex; flex-direction: column; overflow: hidden; padding: 0; }
.knowledge-detail-drawer .el-drawer__footer { padding: 12px 20px; border-top: 1px solid var(--astra-border); background: var(--astra-bg-panel, #fff); }
</style>
