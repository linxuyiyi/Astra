import assert from 'node:assert/strict'
import test from 'node:test'
import { reactive } from 'vue'
import { useFormGuard } from '../src/composables/useFormGuard.ts'

function setup() {
  const state = reactive({ open: true, saving: false, name: '', files: [{ content: '' }] })
  let prompts = 0
  let answer: (accepted: boolean) => void = () => {}
  const guard = useFormGuard({
    isOpen: () => state.open,
    isSaving: () => state.saving,
    snapshot: () => JSON.stringify({ name: state.name, files: state.files }),
    confirmDiscard: () => { prompts++; return new Promise(resolve => { answer = resolve }) },
  })
  guard.capture()
  return { state, guard, answer: (accepted: boolean) => answer(accepted), prompts: () => prompts }
}

test('untouched and reverted forms close without a prompt', async () => {
  const { state, guard, prompts } = setup()
  assert.equal(await guard.canClose(), true)
  state.name = '草稿'
  assert.equal(guard.dirty.value, true)
  state.name = ''
  assert.equal(await guard.canClose(), true)
  assert.equal(prompts(), 0)
})

test('continuing editing preserves nested resource content', async () => {
  const { state, guard, answer } = setup()
  state.files[0].content = '尚未保存的资源内容'
  const closing = guard.canClose()
  answer(false)
  assert.equal(await closing, false)
  assert.equal(guard.dirty.value, true)
  assert.equal(state.files[0].content, '尚未保存的资源内容')
})

test('explicit discard allows closing a changed form', async () => {
  const { state, guard, answer } = setup()
  state.name = '草稿'
  const closing = guard.canClose()
  answer(true)
  assert.equal(await closing, true)
})

test('saving blocks close even when the form is untouched', async () => {
  const { state, guard, prompts } = setup()
  state.saving = true
  assert.equal(await guard.canClose(), false)
  assert.equal(prompts(), 0)
})

test('repeated close actions share a single pending confirmation', async () => {
  const { state, guard, answer, prompts } = setup()
  state.name = '草稿'
  const first = guard.canClose()
  const second = guard.canClose()
  assert.equal(first, second)
  assert.equal(prompts(), 1)
  answer(false)
  assert.deepEqual(await Promise.all([first, second]), [false, false])
})

test('an old confirmation cannot close a newly initialized form', async () => {
  const { state, guard, answer } = setup()
  state.name = '旧草稿'
  const closing = guard.canClose()
  state.name = '新窗口'
  guard.capture()
  answer(true)
  assert.equal(await closing, false)
  assert.equal(guard.dirty.value, false)
})

test('saving that starts during confirmation still blocks closing', async () => {
  const { state, guard, answer } = setup()
  state.name = '草稿'
  const closing = guard.canClose()
  state.saving = true
  answer(true)
  assert.equal(await closing, false)
})
