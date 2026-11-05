import assert from 'node:assert/strict'
import test from 'node:test'
import {workerDisplayName} from '../src/utils/workerName.ts'
test('Worker name comes directly from the unified configuration',()=>{
 assert.equal(workerDisplayName({id:'w1',name:'构建机 01'}),'构建机 01')
 assert.equal(workerDisplayName({id:'w2',name:'测试机 02'}),'测试机 02')
 assert.equal(workerDisplayName(undefined),'未分配 Worker')
})
