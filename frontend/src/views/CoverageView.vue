<template>
  <section class="page" data-testid="coverage-page">
    <header class="page-head">
      <div>
        <h2>识别区域覆盖图</h2>
        <p class="page-desc">
          版式还原只有图片区域边界；覆盖图读取 OCR 识别数据，按页标出已识别、待识别、待确认与明确无文字的区域。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="reload" type="button" @click="reload">重新读取</button>
        <button class="btn" data-testid="reset-revised" type="button" :disabled="busy" @click="resetToRevised">
          退回改版后
        </button>
        <button class="btn" data-testid="supplement" type="button" :disabled="busy" @click="supplement">
          {{ busy ? '处理中…' : '补识别待识别区域' }}
        </button>
        <button class="btn primary" data-testid="reorganize" type="button" :disabled="busy" @click="reorganize">
          布局重排
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="coverage-error">{{ errorMessage }}</p>
    <p v-if="actionMessage" class="coverage-toast" data-testid="coverage-message">{{ actionMessage }}</p>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value" :data-testid="`stat-${item.key}`">{{ item.value }}</strong>
      </article>
    </div>

    <p class="coverage-legend">
      <span v-for="item in legend" :key="item.key" class="legend-item">
        <i class="legend-swatch" :class="`state-${item.key}`"></i>{{ item.label }}
      </span>
      <span class="legend-item"><i class="legend-swatch legend-overlap"></i>区域交叠</span>
    </p>

    <div class="coverage-stages" data-testid="coverage-stages">
      <button
        v-for="stage in stageTabs"
        :key="stage.key"
        class="btn coverage-stage"
        :class="{ active: activeStage === stage.key }"
        :data-testid="`stage-${stage.key}`"
        type="button"
        @click="selectStage(stage.key)"
      >
        {{ stage.label }}
        <small v-if="stageSummary(stage.key)">{{ stageSummary(stage.key) }}</small>
      </button>
    </div>

    <p class="coverage-note" data-testid="coverage-note">
      {{ activeView?.note || '选择阶段查看覆盖视图' }}
      <span v-if="activeView"> · 版式 {{ activeView.version }} · 可确认文本 {{ activeView.summary.reading_blocks }} 块 · 交叠去重 {{ activeView.summary.duplicate_blocks_removed }} 块</span>
    </p>

    <article v-for="page in activeView?.pages ?? []" :key="page.page" class="coverage-page-card">
      <header class="coverage-page-head">
        <h3>第 {{ page.page }} 页</h3>
        <span class="coverage-page-meta">
          已识别 {{ page.state_counts.recognized }} · 待识别 {{ page.state_counts.pending }} ·
          待确认 {{ page.state_counts.unconfirmed }} · 无文字 {{ page.state_counts.no_text }} ·
          阅读顺序 {{ page.reading_order.length }} 块
        </span>
      </header>

      <div class="coverage-canvas" :data-testid="`page-canvas-${page.page}`">
        <div
          v-for="region in page.regions"
          :key="region.region_id"
          class="coverage-region"
          :class="`state-${region.state_key}`"
          :style="regionStyle(region.bbox)"
          :data-testid="`region-${region.region_id}`"
          :data-state="region.state_key"
          :title="`${region.region_id} · ${region.state} · ${region.reason}`"
        >
          <span class="coverage-region-tag">{{ region.region_id }} · {{ region.state }}</span>
          <span v-if="region.state_key === 'recognized'" class="coverage-region-text">
            {{ region.recognized_text }}
          </span>
          <span v-else class="coverage-region-hint">{{ region.reason }}</span>
          <span v-if="region.reused" class="coverage-region-badge">沿用旧识别</span>
        </div>

        <div
          v-for="(pair, index) in page.overlap_pairs"
          :key="`overlap-${index}`"
          class="coverage-overlap"
          :style="regionStyle(pair.bbox)"
          :title="`${pair.a} 与 ${pair.b} 交叠`"
        ></div>

        <div
          v-for="block in page.duplicates"
          :key="`dup-${block.text}-${block.bbox.join('-')}`"
          class="coverage-duplicate"
          :style="regionStyle(block.bbox)"
          :data-testid="`duplicate-${page.page}-${block.text}`"
          :title="`${block.text}：${block.reason}（保留在 ${block.kept_in_region_id}）`"
        >
          去重 {{ block.text }}
        </div>
      </div>

      <div class="coverage-reading" :data-testid="`reading-${page.page}`">
        <h4>本页阅读顺序</h4>
        <ol v-if="page.reading_order.length">
          <li v-for="block in page.reading_order" :key="block.seq">
            <span class="coverage-seq">{{ block.seq }}</span>
            {{ block.text }}
            <small>{{ block.region_id }} · 置信度 {{ block.confidence.toFixed(2) }}<template v-if="block.reused"> · 沿用旧识别</template></small>
          </li>
        </ol>
        <p v-else class="empty-state">本页暂无已确认文本</p>
      </div>

      <div v-if="page.pending_regions.length" class="coverage-pending" :data-testid="`pending-${page.page}`">
        <h4>尚待识别 / 待确认坐标</h4>
        <ul>
          <li v-for="item in page.pending_regions" :key="item.region_id">
            {{ item.region_id }} · {{ item.state }} · {{ formatBbox(item.bbox) }} · {{ item.reason }}
          </li>
        </ul>
      </div>
    </article>

    <p v-if="activeView && !activeView.pages.length" class="empty-state" data-testid="coverage-empty">
      该文档没有可用的图片区域边界
    </p>

    <footer class="page-foot">
      <span>文档 {{ docId || '—' }} · 阶段 {{ activeStageLabel }}</span>
      <span v-if="activeView">可确认文本 {{ activeView.summary.reading_blocks }} 块，待处理区域 {{ activeView.summary.pending_regions }} 个</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  loadDemoIndex,
  loadStage,
  loadStages,
  reorganizeLayout,
  resetDemoToRevised,
  supplementRegions,
} from '@/api/coverage-service'
import type { Bbox, CoverageStage, CoverageView } from '@/data/types'

type StageKey = 'original' | 'revised' | 'supplemented'

const docId = ref('')
const stageTabs = ref<{ key: StageKey; label: string }[]>([
  { key: 'original', label: '旧布局' },
  { key: 'revised', label: '改版后' },
  { key: 'supplemented', label: '补识别后' },
])
const activeStage = ref<StageKey>('supplemented')
const views = ref<Partial<Record<StageKey, CoverageView>>>({})
const errorMessage = ref('')
const actionMessage = ref('')
const busy = ref(false)

const activeView = computed(() => views.value[activeStage.value])
const activeStageLabel = computed(
  () => stageTabs.value.find((item) => item.key === activeStage.value)?.label ?? '—',
)
const legend = [
  { key: 'recognized', label: '已识别（可进入阅读顺序）' },
  { key: 'pending', label: '待识别（坐标保留）' },
  { key: 'unconfirmed', label: '待确认（不能直接采用）' },
  { key: 'no_text', label: '明确无文字' },
]
const statCards = computed(() => {
  const summary = activeView.value?.summary
  return [
    { key: 'regions', label: '图片区域', value: summary?.regions ?? 0 },
    { key: 'recognized', label: '已识别', value: summary?.recognized ?? 0 },
    { key: 'pending', label: '待识别 / 待确认', value: (summary?.pending ?? 0) + (summary?.unconfirmed ?? 0) },
    { key: 'no_text', label: '明确无文字', value: summary?.no_text ?? 0 },
    { key: 'blocks', label: '阅读顺序文本块', value: summary?.reading_blocks ?? 0 },
    { key: 'duplicates', label: '交叠去重', value: summary?.duplicate_blocks_removed ?? 0 },
  ]
})

function stageSummary(key: StageKey): string {
  const summary = views.value[key]?.summary
  if (!summary) return ''
  return `${summary.recognized} 已识别 / ${summary.pending_regions} 待处理`
}

function formatBbox(bbox: Bbox): string {
  return `(${bbox.map((value) => Math.round(value)).join(', ')})`
}

function regionStyle(bbox: Bbox) {
  const [x1, y1, x2, y2] = bbox
  return {
    left: `${x1}px`,
    top: `${y1}px`,
    width: `${Math.max(0, x2 - x1)}px`,
    height: `${Math.max(0, y2 - y1)}px`,
  }
}

async function selectStage(key: StageKey) {
  activeStage.value = key
  if (!views.value[key]) await loadOne(key)
}

async function loadOne(key: StageKey) {
  if (!docId.value) return
  errorMessage.value = ''
  try {
    views.value = { ...views.value, [key]: await loadStage(docId.value, key) }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '覆盖视图读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const index = await loadDemoIndex()
    docId.value = index.doc_id
    if (index.stages.length) {
      stageTabs.value = index.stages.map((item) => ({ key: item.key as StageKey, label: item.label }))
    }
    const payload = await loadStages(docId.value)
    const next: Partial<Record<StageKey, CoverageView>> = {}
    for (const stage of payload.stages as CoverageStage[]) {
      next[stage.key] = stage.view
    }
    views.value = next
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '覆盖数据读取失败'
  }
}

async function resetToRevised() {
  busy.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    await resetDemoToRevised()
    activeStage.value = 'revised'
    actionMessage.value = '已退回改版后：右侧新区域待识别，左侧沿用旧识别'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '演示文档重置失败'
  } finally {
    busy.value = false
  }
}

async function supplement() {
  if (!docId.value) return
  busy.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await supplementRegions(docId.value)
    actionMessage.value = result.message
      ?? `补识别 ${result.recognized_regions} 个区域，覆盖图已更新`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补识别失败'
  } finally {
    busy.value = false
  }
}

async function reorganize() {
  if (!docId.value) return
  busy.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await reorganizeLayout(docId.value)
    actionMessage.value = `布局重排完成：${result.pages} 页，可确认文本 ${result.confirmed_blocks} 块，交叠去重 ${result.duplicates_removed} 块`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '布局重排失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>
