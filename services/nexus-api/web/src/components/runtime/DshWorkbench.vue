<template>
  <section class="dsh-workbench" data-testid="dsh-workbench" :data-binding-id="bindingId">
    <div class="dsh-workbench__bar">
      <ElTag>DSH {{ snapshot?.dsh_version || 'ACP' }}</ElTag>
      <ElText>{{ stateLabel }}</ElText>
      <ElText v-if="workerLabel" type="info">Worker：{{ workerLabel }}</ElText>
      <ElText type="info">Session {{ nativeSessionId.slice(0, 8) }}</ElText>
      <ElButton :loading="loading" @click="refresh">刷新执行过程</ElButton>
      <ElButton @click="$emit('changes')">文件变更</ElButton>
      <ElButton v-if="!readonly && ['running', 'unknown'].includes(snapshot?.state || '')" :loading="sending" @click="stopCurrent">停止当前回复</ElButton>
    </div>
    <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <ElAlert v-if="snapshot?.state === 'unknown'" title="执行结果未知，保留 Worker 占用。请先重连核对，必要时停止当前回复。" type="warning" :closable="false" />
    <div class="dsh-workbench__layout">
      <div class="dsh-workbench__conversation">
        <ElTabs v-model="tab" class="dsh-workbench__tabs">
          <ElTabPane label="执行过程" name="process">
            <div class="dsh-workbench__history">
              <ElEmpty v-if="!records.length && !loading" description="尚无已持久化的执行记录" />
              <article v-for="record in displayRecords" :key="`${nativeSessionId}:${record.seq}`" class="dsh-record" :data-kind="record.kind">
                <div class="dsh-record__label">{{ record.role === 'user' ? '使用者' : record.tool || (record.role === 'assistant' ? 'DSH' : record.kind === 'turn/end' ? '执行状态' : '工具结果') }}<ElTag v-if="record.status" size="small" type="info">{{ statusLabel(record.status) }}</ElTag></div>
                <details v-if="record.role === 'user' && record.text.length > 600"><summary>{{ record.text.slice(0, 160) }}… 展开完整输入</summary><pre>{{ record.text }}</pre></details>
                <pre v-else>{{ record.text }}</pre>
              </article>
              <article v-if="liveText && snapshot?.state === 'running'" class="dsh-record" aria-live="polite">
                <div class="dsh-record__label">DSH 正在回复</div><pre>{{ liveText }}</pre>
              </article>
              <div v-if="snapshot?.state === 'running' && liveTools.length" class="dsh-live-tools">
                <ElText v-for="tool in liveTools" :key="String(tool.toolCallId)">{{ tool.title || tool.toolCallId }} · {{ tool.status || 'pending' }}</ElText>
              </div>
              <ElButton v-if="hasMore" @click="readHistory">读取后续记录</ElButton>
            </div>
          </ElTabPane>
          <ElTabPane label="文件" name="files">
            <div class="dsh-file-search"><ElInput v-model="filePath" placeholder="工作区内相对路径" aria-label="文件路径" @keyup.enter="readFile" /><ElButton :loading="fileLoading" @click="readFile">读取</ElButton></div>
            <ElAlert v-if="fileError" :title="fileError" type="error" :closable="false" />
            <pre class="dsh-file-content">{{ fileContent }}</pre>
          </ElTabPane>
          <ElTabPane label="验证与结果" name="result">
            <ElAlert :title="readonly ? '验证结论以真实执行输出与文件 Diff 为依据。' : '每轮回复结束后仍需在任务顶部人工确认完成。验证结论以真实执行输出与文件 Diff 为依据。'" type="info" :closable="false" />
            <pre class="dsh-file-content">{{ lastReply || '当前轮尚无持久化的最终回复。' }}</pre>
          </ElTabPane>
        </ElTabs>
        <form v-if="!readonly" class="dsh-composer" @submit.prevent="send">
          <ElInput v-model="draft" type="textarea" :rows="3" :maxlength="100000" aria-label="继续当前 DSH Session" placeholder="补充要求或继续当前 Session" :disabled="sending" />
          <div class="dsh-composer__actions"><ElText type="info">{{ canSend ? '沿用当前 Session 与工作区' : '当前轮执行或状态核对中' }}</ElText><ElButton type="primary" native-type="submit" :disabled="!draft.trim() || !canSend" :loading="sending">发送</ElButton></div>
        </form>
      </div>
      <aside class="dsh-workbench__interactions">
        <h3>人工交互</h3>
        <ElEmpty v-if="!permissions.length" :image-size="52" description="没有待处理的工具审批" />
        <article v-for="permission in permissions" :key="permission.id" class="dsh-permission">
          <strong>{{ permission.tool_call.title || '工具操作授权' }}</strong>
          <pre>{{ JSON.stringify(permission.tool_call.rawInput || permission.tool_call, null, 2) }}</pre>
          <ElButton v-for="option in permission.options.filter(item => ['allow_once', 'reject_once'].includes(item.kind))" :key="option.optionId" :type="option.kind === 'reject_once' ? 'default' : 'warning'" :disabled="readonly || sending || Boolean(error)" @click="reply(permission.id, option.optionId)">{{ option.kind === 'allow_once' ? '本次允许' : '本次拒绝' }}</ElButton>
        </article>
        <ElText type="info">此工作台支持消息、工具、审批和文件。DSH 原生计划、待办与终端尚未接入。</ElText>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { ElAlert, ElButton, ElEmpty, ElInput, ElTabPane, ElTabs, ElTag, ElText } from 'element-plus'
import { request } from '@/api/client'

const props = defineProps<{ taskId: string; bindingId: string; nativeSessionId: string; readonly: boolean }>()
const emit = defineEmits<{ changed: []; changes: [] }>()
interface EventRecord { seq: number; kind: string; text: string; role: string; tool: string | null; status: string | null; tool_call_id: string | null }
interface Approval { id: string; tool_call: Record<string, unknown>; options: Array<{ optionId: string; kind: string }> }
interface Snapshot { state: string; dsh_version: string; stream_id: string; submission_id: string | null; event_cursor: number; event_gap: boolean; events: Array<{ seq: number; submission_id: string | null; kind: string; payload: Record<string, unknown> }>; permissions: Approval[] }
const records = ref<EventRecord[]>([]), snapshot = ref<Snapshot | null>(null)
const workerLabel = ref('')
const draft = ref(''), error = ref(''), tab = ref('process'), liveText = ref('')
const loading = ref(false), sending = ref(false), hasMore = ref(false)
const filePath = ref(''), fileContent = ref(''), fileError = ref(''), fileLoading = ref(false)
const tools = ref<Record<string, Record<string, unknown>>>({})
const permissions = computed(() => snapshot.value?.permissions || [])
const liveTools = computed(() => Object.values(tools.value).filter(item => !['completed', 'failed'].includes(String(item.status))))
const displayRecords = computed(() => {
  const results = new Map(records.value.filter(item => item.kind === 'tool/result' && item.tool_call_id).map(item => [item.tool_call_id, item.status]))
  return records.value.map(item => item.kind === 'tool/call' && results.has(item.tool_call_id) ? { ...item, status: results.get(item.tool_call_id) || item.status } : item)
})
const canSend = computed(() => !props.readonly && !sending.value && !error.value && ['prepared', 'succeeded', 'stopped', 'interrupted'].includes(snapshot.value?.state || '') && !permissions.value.length)
const stateLabel = computed(() => error.value ? '连接异常，状态待确认' : permissions.value.length ? '等待工具审批' : ({ prepared: '等待输入', running: '执行中', succeeded: props.readonly ? '当前轮已结束' : '当前轮已结束，等待验收', interrupted: '当前轮已中断', stopped: props.readonly ? '已停止' : '已停止，可继续', unknown: '状态未知' }[snapshot.value?.state || 'unknown']))
const lastReply = computed(() => [...records.value].reverse().find(item => item.kind === 'assistant/message')?.text)
const base = `/api/tasks/${encodeURIComponent(props.taskId)}/workbench/${encodeURIComponent(props.bindingId)}`
const query = `native_session_id=${encodeURIComponent(props.nativeSessionId)}`
let alive = true, cursor = 0, eventCursor = 0, streamId = '', reading = false, taskStateKey = ''
let timer: ReturnType<typeof setTimeout> | undefined

function statusLabel(value: string) { return ({ running: '执行中', completed: '已完成', failed: '失败', settled: '已结束' } as Record<string, string>)[value] || value }
async function readHistory() {
  if (reading || !alive) return
  reading = true
  try {
    const page = await request<{ events: EventRecord[]; next_cursor: number | null; event_count: number }>(`${base}/trace?${query}&cursor=${cursor}`)
    if (!alive) return
    const merged = new Map(records.value.map(item => [item.seq, item]))
    page.events.forEach(item => merged.set(item.seq, item))
    records.value = [...merged.values()].sort((a, b) => a.seq - b.seq).slice(-200)
    cursor = page.next_cursor ?? page.event_count
    hasMore.value = page.next_cursor !== null
  } catch (cause) {
    if (alive) error.value = cause instanceof Error ? cause.message : '原生轨迹暂不可读'
  } finally { reading = false }
}

async function refresh() {
  if (loading.value || !alive) return
  loading.value = true
  try {
    const view = await request<{ snapshot: Snapshot; task_state: Record<string, unknown>; worker: { name?: string } }>(`${base}?${query}&after=${eventCursor}`)
    if (!alive) return
    const value = view.snapshot
    workerLabel.value = view.worker.name || ''
    if (streamId !== value.stream_id || value.event_gap) { liveText.value = ''; tools.value = {}; eventCursor = 0 }
    if (snapshot.value?.state !== value.state || snapshot.value?.submission_id !== value.submission_id) { liveText.value = ''; tools.value = {} }
    const nextTaskKey = JSON.stringify(view.task_state)
    if (taskStateKey !== nextTaskKey) { taskStateKey = nextTaskKey; emit('changed') }
    for (const event of value.events) {
      if (event.seq <= eventCursor || event.submission_id !== value.submission_id) continue
      if (event.kind === 'agent_message_chunk') {
        const content = event.payload.content as { type?: string; text?: string } | undefined
        if (content?.type === 'text') liveText.value = (liveText.value + (content.text || '')).slice(-100000)
      } else if (['tool_call', 'tool_call_update'].includes(event.kind)) {
        const id = String(event.payload.toolCallId)
        tools.value[id] = { ...tools.value[id], ...event.payload }
      }
      eventCursor = event.seq
    }
    streamId = value.stream_id; eventCursor = value.event_cursor; snapshot.value = value; error.value = ''
    await readHistory()
  } catch (cause) { if (alive) error.value = cause instanceof Error ? cause.message : '工作台连接失败' }
  finally { loading.value = false }
}

async function send() {
  if (!canSend.value || !draft.value.trim()) return
  sending.value = true
  try {
    await request(`${base}/input`, { method: 'POST', body: JSON.stringify({ native_session_id: props.nativeSessionId, text: draft.value }) })
    if (alive) { draft.value = ''; emit('changed'); await refresh() }
  } catch (cause) { if (alive) error.value = String(cause instanceof Error ? cause.message : cause) }
  finally { sending.value = false }
}
async function reply(permissionId: string, optionId: string) {
  sending.value = true
  try { await request(`${base}/permission`, { method: 'POST', body: JSON.stringify({ native_session_id: props.nativeSessionId, permission_id: permissionId, option_id: optionId }) }); await refresh() }
  catch (cause) { if (alive) error.value = String(cause instanceof Error ? cause.message : cause) }
  finally { sending.value = false }
}
async function stopCurrent() {
  sending.value = true
  try { await request(`${base}/action`, { method: 'POST', body: JSON.stringify({ action: 'stop-current', native_session_id: props.nativeSessionId }) }); emit('changed'); await refresh() }
  catch (cause) { if (alive) error.value = String(cause instanceof Error ? cause.message : cause) }
  finally { sending.value = false }
}
async function readFile() {
  if (!filePath.value.trim()) return
  fileLoading.value = true; fileError.value = ''; fileContent.value = ''
  try { const result = await request<{ content: string }>(`${base}/file?${query}&path=${encodeURIComponent(filePath.value)}`); if (alive) fileContent.value = result.content }
  catch (cause) { if (alive) fileError.value = String(cause instanceof Error ? cause.message : cause) }
  finally { fileLoading.value = false }
}
async function poll() { await refresh(); if (alive) timer = setTimeout(poll, 2000) }
void poll()
onBeforeUnmount(() => { alive = false; clearTimeout(timer) })
</script>

<style scoped>
.dsh-workbench{display:flex;flex-direction:column;flex:1;min-height:0;gap:12px;height:100%}.dsh-workbench__bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.dsh-workbench__layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,320px);min-height:0;flex:1;gap:20px}.dsh-workbench__conversation{display:flex;flex-direction:column;min-height:0;min-width:0}.dsh-workbench__tabs{flex:1;min-height:0;overflow:hidden}.dsh-workbench__tabs :deep(.el-tabs__content){min-height:0;overflow:auto}.dsh-record summary{cursor:pointer;white-space:pre-wrap;color:var(--el-text-color-secondary)}.dsh-workbench__history{display:flex;flex-direction:column;gap:16px;padding:8px}.dsh-record{border-bottom:1px solid var(--el-border-color-lighter);padding-bottom:12px;min-width:0}.dsh-record__label{display:flex;align-items:center;gap:10px;color:var(--el-text-color-secondary);font-size:13px}.dsh-record pre,.dsh-permission pre,.dsh-file-content{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.65;margin:10px 0}.dsh-live-tools{display:flex;flex-direction:column;gap:8px}.dsh-composer{border-top:1px solid var(--el-border-color);padding:12px 0}.dsh-composer__actions{display:flex;align-items:center;justify-content:space-between;margin-top:8px}.dsh-workbench__interactions{border-left:1px solid var(--el-border-color-lighter);padding-left:20px;overflow:auto}.dsh-workbench__interactions h3{font-size:15px;margin:8px 0 16px}.dsh-permission{margin-bottom:20px}.dsh-permission pre{max-height:260px;overflow:auto}.dsh-file-search{display:flex;gap:8px}@media(max-width:1000px){.dsh-workbench__layout{grid-template-columns:minmax(0,1fr)}.dsh-workbench__interactions{border-left:0;border-top:1px solid var(--el-border-color-lighter);padding:12px 0;max-height:220px}}
</style>
