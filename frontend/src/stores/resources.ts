import { reactive } from 'vue'

import { errorMessage } from '@/api/errors'
import { getResourceKind } from '@/api/resources'
import type { CatalogField, ResourceCatalog, ResourceKind } from '@/api/types'

// 三类真实资源目录的共享状态：列表技能名、档案资源名与抽屉选择共用同一份，
// 避免重复请求。目录不可用绝不静默清空关联，只标记状态。
const API_KIND: Record<CatalogField, ResourceKind> = {
  skills: 'skill',
  mcps: 'mcp',
  knowledge: 'knowledge',
}

export interface ResourceKindState {
  loading: boolean
  loaded: boolean
  error: string | null
  catalog: ResourceCatalog | null
}

function freshState(): ResourceKindState {
  return { loading: false, loaded: false, error: null, catalog: null }
}

const state = reactive<Record<CatalogField, ResourceKindState>>({
  skills: freshState(),
  mcps: freshState(),
  knowledge: freshState(),
})

export function resourceState(field: CatalogField): ResourceKindState {
  return state[field]
}

export async function loadResource(
  field: CatalogField,
  force = false,
): Promise<void> {
  const current = state[field]
  if (current.loading) return
  if (current.loaded && !force) return

  current.loading = true
  current.error = null
  try {
    const payload = await getResourceKind(API_KIND[field])
    const catalog = payload[field]
    if (!catalog) {
      throw new Error('资源目录响应缺少对应投影。')
    }
    current.catalog = catalog
    current.loaded = true
  } catch (error) {
    current.catalog = null
    current.loaded = false
    current.error = errorMessage(error)
  } finally {
    current.loading = false
  }
}

export function loadAllResources(force = false): Promise<void> {
  return Promise.all([
    loadResource('skills', force),
    loadResource('mcps', force),
    loadResource('knowledge', force),
  ]).then(() => undefined)
}

// 资源 ID → 展示名；目录缺失时退回稳定 ID，不伪造名称。
export function resourceName(field: CatalogField, id: string): string {
  const items = state[field].catalog?.items
  const match = items?.find((item) => item.id === id)
  return match?.name || id
}
