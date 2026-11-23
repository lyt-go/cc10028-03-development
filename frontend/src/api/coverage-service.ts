/** 识别区域覆盖图接口封装。 */
import { request } from './client'
import type { CoverageStage, CoverageView } from '@/data/types'

export type DemoIndex = {
  doc_id: string
  versions: string[]
  stages: { key: string; label: string }[]
  overlap_note: string
}

export type SupplementResult = {
  doc_id: string
  version: string
  recognized_regions: number
  message?: string
  view: CoverageView
}

export type ReorganizeResult = {
  doc_id: string
  version: string
  pages: number
  confirmed_blocks: number
  pending_regions: number
  duplicates_removed: number
  layout_updated: boolean
  output: string
  view: CoverageView
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload?.detail) detail = payload.detail
    } catch {
      // 保留兜底文案
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export async function loadDemoIndex(): Promise<DemoIndex> {
  return readJson<DemoIndex>(await request('/api/layout-coverage/demo'), '覆盖演示文档读取失败')
}

export async function loadStages(docId: string): Promise<{ doc_id: string; stages: CoverageStage[] }> {
  return readJson(
    await request(`/api/layout-coverage/${encodeURIComponent(docId)}/stages`),
    '覆盖视图读取失败',
  )
}

export async function loadStage(docId: string, stage: string): Promise<CoverageView> {
  const query = new URLSearchParams({ stage }).toString()
  return readJson<CoverageView>(
    await request(`/api/layout-coverage/${encodeURIComponent(docId)}?${query}`),
    '覆盖视图读取失败',
  )
}

export async function resetDemoToRevised(): Promise<{ doc_id: string; stage: string }> {
  return readJson<{ doc_id: string; stage: string }>(
    await request('/api/layout-coverage/demo/revised', { method: 'POST' }),
    '演示文档重置失败',
  )
}

export async function supplementRegions(docId: string): Promise<SupplementResult> {
  return readJson<SupplementResult>(
    await request(`/api/layout-coverage/${encodeURIComponent(docId)}/supplement`, { method: 'POST' }),
    '补识别失败',
  )
}

export async function reorganizeLayout(docId: string): Promise<ReorganizeResult> {
  return readJson<ReorganizeResult>(
    await request(`/api/layout-coverage/${encodeURIComponent(docId)}/reorganize`, { method: 'POST' }),
    '布局重排失败',
  )
}
