import assert from 'node:assert/strict'
import test from 'node:test'

import { buildScene, collaborationKey, galaxyColor, hash32 } from '../src/features/nebula/layout.ts'
import type { NebulaEmployee, NebulaSnapshot, NebulaTask, NebulaWorker } from '../src/features/nebula/types.ts'

function employee(id: string, overrides: Partial<NebulaEmployee> = {}): NebulaEmployee {
  return {
    id,
    name: `员工 ${id}`,
    domain: '测试',
    description: '测试职责',
    avatar: '✦',
    enabled: true,
    interactionStatus: 'available',
    counts: { running: 1, queued: 1, waiting: 0, failed: 0, completedToday: 0 },
    workerIds: [`w-${id}`],
    createdAt: '2026-01-01T00:00:00+00:00',
    ...overrides,
  }
}

function worker(id: string, owner: string, overrides: Partial<NebulaWorker> = {}): NebulaWorker {
  return {
    id,
    digitalEmployeeId: owner,
    workerInstanceId: null,
    name: `Worker ${id}`,
    role: '执行',
    capabilityLabels: ['执行'],
    environmentMode: 'LOCAL',
    environmentName: '本地',
    modelName: null,
    health: 'online',
    occupancy: 'running',
    taskCount: 1,
    runningTaskCount: 1,
    waitingTaskCount: 0,
    lastHeartbeatAt: null,
    ...overrides,
  }
}

function task(id: string, owner: string, workerId: string | null, status: NebulaTask['status'] = 'RUNNING'): NebulaTask {
  return {
    id,
    title: id,
    status,
    priority: 'MEDIUM',
    taskType: 'ISSUE',
    responsibleEmployeeId: owner,
    workerId,
    workerName: workerId ? `Worker ${workerId}` : null,
    ownerId: owner,
    ownerName: `员工 ${owner}`,
    currentProgress: '',
    waitingReason: '',
    failureReason: '',
    runtimeConnection: 'CONNECTED',
    currentSessionId: null,
    sessionStatus: null,
    pendingMessageId: null,
    sourceConversationId: null,
    sourceChannelId: null,
    inactivityAlert: false,
    conflict: false,
    conflictCodes: [],
    createdAt: '2026-01-01T00:00:00+00:00',
    updatedAt: '2026-01-01T00:00:00+00:00',
    completedAt: null,
  }
}

function snapshot(employees: NebulaEmployee[], workers: NebulaWorker[], tasks: NebulaTask[]): NebulaSnapshot {
  return {
    snapshotVersion: 'v1',
    generatedAt: '2026-01-01T00:00:00+00:00',
    timezone: 'Asia/Shanghai',
    degradedSources: [],
    totals: {
      employees: employees.length,
      workers: workers.length,
      workersOnline: workers.filter((item) => item.health === 'online').length,
      workersOffline: 0,
      tasksRunning: 1,
      tasksQueued: 1,
      tasksWaiting: 0,
      tasksFailed: 0,
      completedToday: 0,
    },
    employees,
    workers,
    taskEdges: [
      {
        digitalEmployeeId: 'a',
        workerId: 'w-a',
        running: 1,
        waiting: 0,
        unavailable: 0,
        taskIdsPreview: ['T-1'],
      },
    ],
    collaborationEdges: [],
    queuedTasks: tasks.filter((item) => item.status === 'PENDING'),
    attentionTasks: [],
    unassignedTasks: [],
    unassigned: { workerCount: 0, taskCount: 0, counts: { running: 0, queued: 0, waiting: 0, failed: 0, completedToday: 0 } },
    dataConflicts: [],
    dataWarnings: [],
  }
}

test('hash32 and collaborationKey are deterministic', () => {
  assert.equal(hash32('employee-a'), hash32('employee-a'))
  assert.equal(collaborationKey('b', 'a'), collaborationKey('a', 'b'))
  assert.equal(collaborationKey('b', 'a'), 'a:b')
})

test('task status changes do not move galaxies, workers or satellites', () => {
  const employees = [employee('a'), employee('b')]
  const workers = [worker('w-a', 'a'), worker('w-b', 'b')]
  const before = buildScene(
    snapshot(employees, workers, [
      task('T-1', 'a', 'w-a'),
      task('T-2', 'a', null, 'PENDING'),
    ]),
  )
  const after = buildScene(
    snapshot(
      employees.map((item) => ({
        ...item,
        counts: { ...item.counts, running: 2, waiting: 1, failed: 1 },
      })),
      workers,
      [
        task('T-1', 'a', 'w-a', 'WAITING'),
        task('T-3', 'a', 'w-a'),
        task('T-2', 'a', null, 'PENDING'),
      ],
    ),
  )

  for (const id of ['a', 'b']) {
    assert.deepEqual(after.galaxyById.get(id)?.position, before.galaxyById.get(id)?.position)
  }
  assert.deepEqual(after.workerNodeById.get('w-a')?.position, before.workerNodeById.get('w-a')?.position)
  assert.deepEqual(
    after.satellites.map((item) => item.position),
    before.satellites.map((item) => item.position),
  )
})

test('galaxy color follows business state instead of decorative palette', () => {
  const palette = '#21d5ab'
  assert.equal(galaxyColor(employee('a'), palette), '#5df1b8')
  assert.equal(
    galaxyColor(employee('a', { counts: { running: 0, queued: 0, waiting: 1, failed: 0, completedToday: 0 } }), palette),
    '#ff9470',
  )
  assert.equal(
    galaxyColor(employee('a', { counts: { running: 0, queued: 0, waiting: 0, failed: 1, completedToday: 0 } }), palette),
    '#fb6484',
  )
  assert.equal(
    galaxyColor(employee('a', { counts: { running: 0, queued: 0, waiting: 0, failed: 0, completedToday: 0 } }), palette),
    palette,
  )
})

test('only owned workers orbit a galaxy and queued satellites cap at five', () => {
  const employees = [
    employee('a', {
      counts: { running: 0, queued: 7, waiting: 0, failed: 0, completedToday: 0 },
    }),
  ]
  const workers = [worker('w-a', 'a'), worker('w-x', null)]
  const queued = Array.from({ length: 7 }, (_, index) => task(`T-${index}`, 'a', null, 'PENDING'))
  const scene = buildScene(snapshot(employees, workers, queued))
  assert.equal(scene.workers.length, 1)
  assert.equal(scene.galaxies[0].queue.length, 5)
  assert.equal(scene.galaxies[0].queuedCount, 7)
})
