export const WORKSPACE_AREA_IDS = [
  'development', 'collaboration', 'pantry', 'integration', 'meeting', 'special',
] as const

export type WorkspaceAreaId = (typeof WORKSPACE_AREA_IDS)[number]
export type CharacterPose = 'standing' | 'seated'

export interface WorkspaceArea {
  id: WorkspaceAreaId
  name: string
  shortName: string
}

/**
 * Business-facing area names remain available to employee settings and task
 * presentation. Visual coordinates and waypoints intentionally live nowhere:
 * the digital office now composes stable DOM workstations by team.
 */
export const workspaceAreas: WorkspaceArea[] = [
  { id: 'development', name: '开发办公区', shortName: '开发办公区' },
  { id: 'collaboration', name: '协作讨论区', shortName: '协作讨论区' },
  { id: 'pantry', name: '茶水间', shortName: '茶水间' },
  { id: 'integration', name: '集成区', shortName: '集成区' },
  { id: 'meeting', name: '会议室', shortName: '会议室' },
  { id: 'special', name: '专项处理区', shortName: '专项处理区' },
]

export const workspaceAreaMap = new Map(workspaceAreas.map((area) => [area.id, area]))

export function isWorkspaceAreaId(value: string | null | undefined): value is WorkspaceAreaId {
  return WORKSPACE_AREA_IDS.includes(value as WorkspaceAreaId)
}
