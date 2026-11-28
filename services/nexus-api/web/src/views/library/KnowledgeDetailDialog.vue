<template>
  <ElDrawer
    v-if="open"
    class="knowledge-detail-drawer"
    data-testid="knowledge-detail-dialog"
    :model-value="true"
    :show-close="false"
    size="var(--astra-drawer-xl)"
    :before-close="() => requestClose()"
  >
    <template #header>
      <div class="kd-header">
        <div class="kd-header__main">
          <div class="kd-header__title-row">
            <ElTag v-if="detail" size="small" type="info" class="astra-tag--neutral">{{ typeLabel(detail.type) }}</ElTag>
            <h2 :id="detail ? `knowledge-${detail.id}` : 'knowledge-detail-title'">
              {{ detail?.title ?? (error ? '知识详情加载失败' : '正在加载知识详情') }}
            </h2>
          </div>
          <div v-if="detail" class="kd-header__meta">
            <ElTag
              size="small"
              :type="statusTagType(detail.status)"
              :class="{ 'astra-tag--neutral': statusTagType(detail.status) === 'info' }"
            >{{ statusLabel(detail.status) }}</ElTag>
            <ElTag
              size="small"
              :type="verificationTagType(detail.verification_status)"
              :class="{ 'astra-tag--neutral': verificationTagType(detail.verification_status) === 'info' }"
              effect="plain"
            >{{ verificationLabel(detail.verification_status) }}</ElTag>
            <span class="kd-meta-sep" aria-hidden="true">·</span>
            <span>{{ spaceName }}</span>
            <span class="kd-meta-sep" aria-hidden="true">·</span>
            <span>更新于 {{ formatDate(detail.updated_at) }}</span>
            <span class="kd-meta-sep" aria-hidden="true">·</span>
            <span>引用 {{ detail.reference_count }} 次</span>
            <template v-if="detail.source_task_id">
              <span class="kd-meta-sep" aria-hidden="true">·</span>
              <a :href="`/tasks/${detail.source_task_id}`" @click="onTaskLinkClick($event, `/tasks/${detail.source_task_id}`)">
                来源任务
              </a>
            </template>
          </div>
          <div v-if="detail?.tags?.length" class="kd-header__tags">
            <span v-for="tag in detail.tags" :key="tag" class="kd-tag-chip"># {{ tag }}</span>
          </div>
        </div>
        <ElButton class="kd-header__close" text :icon="X" aria-label="关闭知识详情" @click="requestClose" />
      </div>
    </template>

    <div class="kd-body">
      <ElAlert v-if="error" class="kd-error" type="error" show-icon :closable="false" :title="error" />
      <ElSkeleton v-if="!detail && !error" class="kd-loading" animated :rows="8" />

      <template v-else-if="detail">
        <div class="kd-tabs-bar">
          <ElRadioGroup v-model="activeTab" size="small" aria-label="详情内容分区">
            <ElRadioButton value="content">{{ reviewEditing ? '发布前编辑' : '正文阅读' }}</ElRadioButton>
            <ElRadioButton v-if="detail.status === 'candidate'" value="evidence">
              来源证据{{ detail.evidence.length ? `（${detail.evidence.length}）` : '' }}
            </ElRadioButton>
            <ElRadioButton value="meta">
              属性与引用{{ usages.length ? `（${usages.length}）` : '' }}
            </ElRadioButton>
          </ElRadioGroup>
          <ElTag v-if="reviewDirty" size="small" type="warning" effect="light">有未保存修改</ElTag>
        </div>

        <div class="kd-scroll">
          <!-- Tab 1: 正文阅读 / 发布前编辑 -->
          <template v-if="activeTab === 'content'">
            <ElAlert
              v-if="detail.status === 'candidate' && detail.review_note"
              class="kd-banner"
              type="warning"
              show-icon
              :closable="false"
              title="审核意见"
              :description="detail.review_note"
            />

            <ElForm v-if="reviewEditing" class="review-form" :model="review" label-position="top">
              <div class="review-form__row">
                <ElFormItem label="目标目录" required>
                  <ElSelect v-model="review.space_id" placeholder="请选择" style="width: 100%">
                    <ElOption v-for="dir in directories" :key="dir.id" :value="dir.id" :label="dir.name" />
                  </ElSelect>
                </ElFormItem>
                <ElFormItem label="验证状态">
                  <ElSelect v-model="review.verification_status" style="width: 100%">
                    <ElOption value="unverified" label="未验证" />
                    <ElOption value="verified" label="已验证" />
                    <ElOption value="failed" label="验证失败" />
                  </ElSelect>
                </ElFormItem>
              </div>
              <div class="review-form__row">
                <ElFormItem label="标题" required>
                  <ElInput v-model.trim="review.title" maxlength="240" show-word-limit />
                </ElFormItem>
                <ElFormItem label="标签">
                  <ElInput v-model="review.tags" placeholder="逗号分隔" />
                </ElFormItem>
              </div>
              <ElFormItem label="摘要">
                <ElInput v-model="review.summary" type="textarea" :rows="2" maxlength="4000" show-word-limit />
              </ElFormItem>
              <ElFormItem label="Markdown 正文" required>
                <ElInput v-model="review.content_md" class="review-form__content" type="textarea" :rows="16" resize="vertical" />
              </ElFormItem>
            </ElForm>

            <template v-else>
              <div v-if="detail.summary" class="kd-summary">
                <span class="kd-summary__label">摘要</span>
                <p>{{ detail.summary }}</p>
              </div>
              <MarkdownView
                :content="detail.content_md || ''"
                show-toc
                allow-source-toggle
                allow-copy
                empty-text="该文档暂无正文内容"
              />
            </template>

            <section v-if="reviewActionMode && detail.status === 'candidate'" ref="reviewRequestPanel" class="review-request-panel">
              <strong>{{ reviewActionMode === 'supplement' ? '退回补充' : '驳回候选经验' }}</strong>
              <ElInput
                ref="reviewReasonInput"
                v-model.trim="review.reason"
                type="textarea"
                :rows="3"
                :disabled="working"
                :placeholder="reviewActionMode === 'supplement' ? '说明需要补充的内容' : '请输入驳回原因'"
              />
              <ElSpace class="review-request-panel__actions" :size="8">
                <ElButton :disabled="working" @click="reviewActionMode = null">取消</ElButton>
                <ElButton
                  :type="reviewActionMode === 'reject' ? 'danger' : 'primary'"
                  :loading="working"
                  :disabled="review.reason.length < 2"
                  @click="confirmReviewAction"
                >{{ reviewActionMode === 'supplement' ? '确认退回补充' : '确认驳回' }}</ElButton>
              </ElSpace>
            </section>
          </template>

          <!-- Tab 2: 候选经验来源证据 -->
          <template v-else-if="activeTab === 'evidence' && detail.status === 'candidate'">
            <ElAlert
              v-if="detail.review_note"
              class="kd-banner"
              type="warning"
              show-icon
              :closable="false"
              title="审核意见"
              :description="detail.review_note"
            />
            <ElDescriptions v-if="candidateFacts.length" class="kd-desc" :column="2" border>
              <ElDescriptionsItem v-for="fact in candidateFacts" :key="fact.label" :label="fact.label">{{ fact.value }}</ElDescriptionsItem>
            </ElDescriptions>
            <ElDescriptions v-if="experienceFields.length" class="kd-desc" :column="2" border>
              <ElDescriptionsItem v-for="field in experienceFields" :key="field.label" :label="field.label">{{ field.value }}</ElDescriptionsItem>
            </ElDescriptions>
            <section class="kd-section">
              <h4>相关日志 / 会话 / 任务证据</h4>
              <ElTable v-if="detail.evidence.length" :data="detail.evidence" border>
                <ElTableColumn label="类型" prop="evidence_type" width="110" />
                <ElTableColumn label="内容" min-width="220">
                  <template #default="{ row }">{{ row.content || row.uri || '已关联结构化证据' }}</template>
                </ElTableColumn>
                <ElTableColumn label="任务" width="130" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.task_id || '—' }}</template>
                </ElTableColumn>
                <ElTableColumn label="结果" width="96">
                  <template #default="{ row }">
                    <ElTag
                      size="small"
                      :type="row.result ? (row.result.includes('fail') || row.result.includes('error') ? 'danger' : 'success') : 'info'"
                      :class="{ 'astra-tag--neutral': !row.result }"
                    >{{ row.result || '未标记' }}</ElTag>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="链接" width="72">
                  <template #default="{ row }">
                    <ElLink v-if="safeExternalUrl(row.uri)" :href="safeExternalUrl(row.uri)" target="_blank" rel="noopener noreferrer" type="primary">打开</ElLink>
                    <span v-else>—</span>
                  </template>
                </ElTableColumn>
              </ElTable>
              <p v-else class="kd-empty-text">暂无额外证据记录</p>
            </section>
          </template>

          <!-- Tab 3: 属性与引用 -->
          <template v-else>
            <ElDescriptions class="kd-desc" :column="2" border>
              <ElDescriptionsItem label="知识 ID"><span class="kd-mono">{{ detail.id }}</span></ElDescriptionsItem>
              <ElDescriptionsItem label="所属目录">{{ spaceName }}</ElDescriptionsItem>
              <ElDescriptionsItem label="知识类型">{{ typeLabel(detail.type) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="来源方式">{{ sourceTypeLabel(detail.source_type) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="更新时间">{{ formatDate(detail.updated_at) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="发布时间">{{ detail.published_at ? formatDate(detail.published_at) : '尚未发布' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="验证时间">{{ detail.verified_at ? formatDate(detail.verified_at) : '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="有帮助 / 引用">{{ detail.helpful_count }} / {{ detail.reference_count }}</ElDescriptionsItem>
              <ElDescriptionsItem v-if="detail.source_employee_name || detail.source_employee_id" label="来源员工">
                {{ detail.source_employee_name || detail.source_employee_id }}
              </ElDescriptionsItem>
              <ElDescriptionsItem v-if="detail.source_session_id" label="执行会话">
                <span class="kd-mono">{{ detail.source_session_id }}</span>
              </ElDescriptionsItem>
            </ElDescriptions>

            <section v-if="detail.sources.length" class="kd-section">
              <h4>资料来源</h4>
              <ElTable :data="detail.sources" border>
                <ElTableColumn label="来源" min-width="200">
                  <template #default="{ row }">{{ row.source_name || row.source_id || row.source_type }}</template>
                </ElTableColumn>
                <ElTableColumn label="类型" width="140">
                  <template #default="{ row }">{{ row.source_type }}<template v-if="row.version"> · {{ row.version }}</template></template>
                </ElTableColumn>
                <ElTableColumn label="链接" width="88">
                  <template #default="{ row }">
                    <ElLink v-if="safeExternalUrl(row.source_uri)" :href="safeExternalUrl(row.source_uri)" target="_blank" rel="noopener noreferrer" type="primary">查看</ElLink>
                    <span v-else>—</span>
                  </template>
                </ElTableColumn>
              </ElTable>
            </section>

            <section class="kd-section">
              <div class="kd-section__head">
                <h4>最近引用</h4>
                <ElButton link type="primary" @click="openRelated">查看关联任务</ElButton>
              </div>
              <ElTable v-if="usages.length" :data="usages" border>
                <ElTableColumn label="任务" min-width="200">
                  <template #default="{ row }">
                    <a v-if="row.task_id" :href="`/tasks/${row.task_id}`" @click="onTaskLinkClick($event, `/tasks/${row.task_id}`)">{{ row.task_title || row.task_id }}</a>
                    <span v-else>{{ row.task_title || '引用' }}</span>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="执行员工" width="130" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.employee_name || row.employee_id || 'Worker' }}</template>
                </ElTableColumn>
                <ElTableColumn label="动作" prop="action" width="100" />
                <ElTableColumn label="引用时间" width="144">
                  <template #default="{ row }">{{ formatDate(row.used_at) }}</template>
                </ElTableColumn>
              </ElTable>
              <p v-else-if="usagesLoading" class="kd-empty-text">正在加载引用记录…</p>
              <p v-else class="kd-empty-text">暂无引用记录</p>
            </section>

            <section v-if="relatedOpen" class="kd-section">
              <div class="kd-section__head">
                <h4>关联任务</h4>
                <ElButton text :icon="X" aria-label="关闭关联任务" @click="relatedOpen = false" />
              </div>
              <ElTable v-if="relatedTasks.length || !relatedLoading" :data="relatedTasks" border>
                <ElTableColumn label="任务" min-width="220">
                  <template #default="{ row }">
                    <a :href="`/tasks/${row.id}`" @click="onTaskLinkClick($event, `/tasks/${row.id}`)">{{ row.title }}</a>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="关系" prop="relation" width="100" />
                <ElTableColumn label="状态" prop="status" width="100" />
                <ElTableColumn label="最近引用" width="144">
                  <template #default="{ row }">{{ row.last_used_at ? formatDate(row.last_used_at) : '—' }}</template>
                </ElTableColumn>
                <template #empty>暂无关联任务</template>
              </ElTable>
              <p v-else class="kd-empty-text">正在加载关联任务…</p>
            </section>
          </template>
        </div>
      </template>
    </div>

    <template v-if="detail" #footer>
      <div class="kd-footer">
        <ElSpace :size="8">
          <ElButton
            size="small"
            :type="detail.is_favorite ? 'warning' : 'default'"
            :plain="!detail.is_favorite"
            :icon="Star"
            :disabled="working"
            data-testid="knowledge-detail-favorite"
            @click="emit('favorite')"
          >{{ detail.is_favorite ? '已收藏' : '收藏' }}</ElButton>
          <ElButton
            size="small"
            :type="detail.user_helpful ? 'primary' : 'default'"
            :plain="!detail.user_helpful"
            :icon="ThumbsUp"
            :disabled="working"
            data-testid="knowledge-detail-helpful"
            @click="emit('helpful')"
          >有帮助（{{ detail.helpful_count }}）</ElButton>
          <ElButton size="small" :icon="Link2" @click="copyLink">{{ copied ? '已复制链接' : '复制链接' }}</ElButton>
        </ElSpace>

        <ElSpace :size="8">
          <template v-if="isReviewFlow">
            <ElButton
              :icon="reviewEditing ? Eye : ClipboardEdit"
              :disabled="working"
              data-testid="knowledge-detail-review-edit"
              @click="toggleReviewEditing"
            >{{ reviewEditing ? '返回阅读' : '发布前编辑' }}</ElButton>
            <template v-if="detail.status === 'candidate'">
              <ElButton type="danger" plain :disabled="working" @click="startReviewAction('reject')">驳回</ElButton>
              <ElButton :disabled="working" @click="startReviewAction('supplement')">退回补充</ElButton>
            </template>
            <ElButton
              type="primary"
              :loading="working"
              :disabled="!canPublish"
              data-testid="knowledge-detail-publish"
              @click="publish"
            >发布</ElButton>
          </template>
          <ElButton
            v-else-if="canManage"
            type="primary"
            :icon="Pencil"
            data-testid="knowledge-detail-edit"
            @click="emit('edit')"
          >编辑</ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { ClipboardEdit, Eye, Link2, Pencil, Star, ThumbsUp, X } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElDescriptions,
  ElDescriptionsItem,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElLink,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSkeleton,
  ElSpace,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus'
import type { InputInstance } from 'element-plus'
import type { KnowledgeDetail, KnowledgeUsageRecord, RelatedTask } from '@/api/types'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import { confirmDialog } from '@/composables/useConfirm'

const props = defineProps<{
  open: boolean
  detail: KnowledgeDetail | null
  directories: Array<{ id: string; name: string; level: number }>
  canReview: boolean
  canManage: boolean
  working: boolean
  error: string
  relatedTasks: RelatedTask[]
  relatedLoading: boolean
  usages: KnowledgeUsageRecord[]
  usagesLoading: boolean
}>()

const emit = defineEmits<{
  close: []
  favorite: []
  helpful: []
  related: []
  edit: []
  publish: [payload: Record<string, unknown>]
  reject: [reason: string]
  supplement: [reason: string]
}>()

const router = useRouter()
const activeTab = ref<'content' | 'evidence' | 'meta'>('content')
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

const isReviewFlow = computed(() => Boolean(props.detail && props.canReview && ['candidate', 'draft'].includes(props.detail.status)))
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
      activeTab.value = 'content'
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

const spaceName = computed(() => {
  const detail = props.detail
  if (!detail) return '—'
  if (detail.space_name) return detail.space_name
  return props.directories.find((dir) => dir.id === detail.space_id)?.name.trim() ?? '未归类'
})
const typeLabel = (value: string) => ({ document: '文档', rule: '规范', guide: '指南', experience: '经验', faq: 'FAQ', tool: '工具', example: '示例' }[value] ?? value)
const statusLabel = (value: string) => ({ published: '已发布', candidate: '待审核', draft: '草稿', archived: '已归档', rejected: '已驳回' }[value] ?? value)
const statusTagType = (value: string) => (({ published: 'success', candidate: 'warning', draft: 'info', archived: 'info', rejected: 'danger' }[value] ?? 'info') as 'success' | 'warning' | 'info' | 'danger')
const verificationLabel = (value: string) => ({ verified: '已验证', unverified: '未验证', failed: '验证失败' }[value] ?? value)
const verificationTagType = (value: string) => (({ verified: 'success', failed: 'danger' }[value] ?? 'info') as 'success' | 'danger' | 'info')
const sourceTypeLabel = (value: string | null) => ({ task: '任务沉淀', manual: '人工创建', import: '文档导入', system: '系统同步' }[value ?? ''] ?? '人工创建')
const formatDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))

function experienceValue(keys: string[]) {
  for (const key of keys) {
    const value = props.detail?.experience_data[key]
    if (value !== undefined && value !== null && value !== '') {
      return typeof value === 'boolean' ? (value ? '通过' : '未通过') : String(value)
    }
  }
  return '—'
}

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
  return [
    { label: '来源员工', value: detail.source_employee_name || detail.source_employee_id || '' },
    { label: '来源任务', value: detail.source_task_title || detail.source_task_id || '' },
    { label: '执行会话', value: detail.source_session_id || '' },
    { label: '证据状态', value: detail.verified_at ? '来源会话已验证' : verificationLabel(detail.verification_status) },
    { label: '执行时间', value: experienceValue(['task_executed_at', 'executed_at']) },
    { label: '处理前', value: experienceValue(['before_status', 'status_before', 'problem_symptom']) },
    { label: '处理后', value: experienceValue(['after_status', 'status_after', 'retry_result', 'run_status']) },
    { label: '验证次数', value: experienceValue(['verification_count', 'retry_count']) },
    { label: '验证结果', value: experienceValue(['verification_passed', 'verification_result']) },
  ].filter((fact) => fact.value && fact.value !== '—')
})

const experienceFields = computed(() => [
  { label: '经验类型', value: experienceValue(['kind', 'experience_type']) },
  { label: '改进对象', value: experienceValue(['target_component']) },
  { label: '建议改动', value: experienceValue(['proposed_change']) },
  { label: '证据等级', value: experienceValue(['evaluation_level']) },
  { label: '问题现象', value: experienceValue(['problem_symptom', 'problem', 'symptom']) },
  { label: '原始异常', value: experienceValue(['original_error', 'error', 'exception']) },
  { label: '处理方式', value: experienceValue(['handling', 'resolution', 'solution']) },
  { label: '修改摘要', value: experienceValue(['change_summary', 'changes', 'modification_summary']) },
  { label: '验证结果', value: experienceValue(['retry_result', 'verification_result', 'result']) },
].filter((field) => field.value !== '—'))

async function discardUnsavedReview(actionLabel: string, confirmLabel: string): Promise<boolean> {
  if (!reviewDirty.value) return true
  return confirmDialog({
    title: '放弃未保存的编辑？',
    message: `发布前编辑的内容尚未提交，${actionLabel}后将丢失修改。`,
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
  if (!reviewEditing.value) {
    activeTab.value = 'content'
    reviewEditing.value = true
    return
  }
  if (!(await discardUnsavedReview('返回阅读', '放弃修改'))) return
  syncReviewFromDetail()
  reviewEditing.value = false
}

async function startReviewAction(mode: 'supplement' | 'reject') {
  if (!(await discardUnsavedReview('该操作不会带上正文编辑', '放弃并继续'))) return
  activeTab.value = 'content'
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
  emit('publish', {
    space_id: review.space_id,
    title: review.title,
    summary: review.summary,
    tags: review.tags.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
    content_md: review.content_md,
    verification_status: review.verification_status,
  })
}

function knowledgeUrl() {
  const url = new URL(window.location.href)
  if (props.detail) url.searchParams.set('knowledge', props.detail.id)
  return url.toString()
}
async function copyLink() {
  try {
    await navigator.clipboard.writeText(knowledgeUrl())
    copied.value = true
    window.clearTimeout(copyTimer)
    copyTimer = window.setTimeout(() => { copied.value = false }, 1600)
  } catch {
    copied.value = false
  }
}
function openRelated() {
  activeTab.value = 'meta'
  relatedOpen.value = true
  emit('related')
}
async function onTaskLinkClick(event: MouseEvent, to: string) {
  event.preventDefault()
  if (!(await discardUnsavedReview('离开详情', '放弃并离开'))) return
  emit('close')
  await router.push(to)
}
</script>

<style scoped>
.kd-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; width: 100%; min-width: 0; }
.kd-header__main { display: grid; gap: 6px; min-width: 0; }
.kd-header__title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.kd-header__title-row h2 { margin: 0; color: var(--astra-text-primary); font-size: 19px; font-weight: 600; line-height: 1.35; }
.kd-header__meta {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px;
  color: var(--astra-text-muted); font-size: 12.5px;
}
.kd-header__meta a { color: var(--astra-accent-text); text-decoration: none; }
.kd-header__meta a:hover { text-decoration: underline; }
.kd-meta-sep { color: var(--astra-border-hover); }
.kd-header__tags { display: flex; flex-wrap: wrap; gap: 6px; }
.kd-tag-chip {
  padding: 1px 7px; border-radius: 5px;
  border: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary); font-size: 11.5px;
}
.kd-header__close { flex: none; }

.kd-body { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.kd-error { margin: 12px 24px 0; }
.kd-loading { padding: 20px 24px; }

.kd-tabs-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 24px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}
.kd-scroll {
  flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
  padding: 18px 24px 28px;
  scrollbar-gutter: stable;
}

.kd-banner { margin-bottom: 14px; }
.kd-summary {
  display: grid; gap: 4px;
  margin-bottom: 16px; padding: 10px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}
.kd-summary__label { color: var(--astra-text-muted); font-size: 11.5px; font-weight: 600; }
.kd-summary p { margin: 0; color: var(--astra-text-secondary); font-size: 13.5px; line-height: 1.6; }

.kd-desc { margin-bottom: 16px; }
.kd-mono { font-family: var(--astra-font-mono); font-size: 12px; color: var(--astra-text-secondary); }
.kd-section { margin-top: 18px; display: grid; gap: 10px; }
.kd-section h4 { margin: 0; color: var(--astra-text-primary); font-size: 14px; font-weight: 600; }
.kd-section__head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.kd-section a { color: var(--astra-accent-text); text-decoration: none; }
.kd-section a:hover { text-decoration: underline; }
.kd-empty-text { margin: 0; color: var(--astra-text-muted); font-size: 13px; }

.review-form { display: grid; gap: 2px; }
.review-form__row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.review-form__content :deep(.el-textarea__inner) { font-family: var(--astra-font-mono); font-size: 13px; line-height: 1.65; }
.review-request-panel {
  display: grid; gap: 10px; margin-top: 18px; padding: 14px;
  border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle);
}
.review-request-panel strong { color: var(--astra-text-primary); font-size: 13px; }
.review-request-panel__actions { justify-content: flex-end; }

.kd-footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 12px; width: 100%; }

@media (max-width: 720px) {
  .review-form__row { grid-template-columns: 1fr; }
  .kd-footer { flex-direction: column; align-items: flex-start; }
}
</style>

<style>
.knowledge-detail-drawer .el-drawer__header { margin-bottom: 0; padding: 16px 24px 14px; border-bottom: 1px solid var(--astra-border); }
.knowledge-detail-drawer .el-drawer__body { display: flex; flex-direction: column; overflow: hidden; padding: 0; }
.knowledge-detail-drawer .el-drawer__footer { padding: 12px 24px; border-top: 1px solid var(--astra-border); background: var(--astra-bg-panel, #fff); }
</style>
