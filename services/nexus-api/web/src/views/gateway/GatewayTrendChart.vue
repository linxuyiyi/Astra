<template>
  <div ref="chartRef" class="gateway-trend" :class="{ 'gateway-trend--tall': tall, 'gateway-trend--compact': compact }" role="img" :aria-label="ariaLabel" />
</template>

<script setup lang="ts">
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useResizeObserver } from '@vueuse/core'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { GatewayStatBucket } from '@/api/gateway'
import { chartTypography, loadChartFonts } from '@/utils/typography'

const props = withDefaults(
  defineProps<{
    buckets?: GatewayStatBucket[]
    points?: Array<{ bucket_start: string; value: number | null }>
    callPoints?: Array<{ bucket_start: string; requests: number; errors: number }>
    mode?: 'tokens' | 'requests' | 'value' | 'calls'
    valueName?: string
    tall?: boolean
    compact?: boolean
  }>(),
  { buckets: () => [], points: () => [], callPoints: () => [], mode: 'tokens', valueName: '数值', tall: false, compact: false },
)

const chartRef = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const ariaLabel = computed(() => {
  if (props.mode === 'tokens') return 'API 网关 Token 使用趋势图'
  if (props.mode === 'value') return `API 网关 ${props.valueName} 趋势图`
  if (props.mode === 'calls') return 'API 网关请求与失败趋势图'
  return 'API 网关请求量趋势图'
})

function parseTime(value: string) {
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`)
}

function axisLabels() {
  const points = props.mode === 'value' ? props.points : props.mode === 'calls' ? props.callPoints : props.buckets
  if (points.length < 2) {
    return points.map((point) => parseTime(point.bucket_start).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }))
  }
  const stepMinutes = (parseTime(points[1].bucket_start).getTime() - parseTime(points[0].bucket_start).getTime()) / 60_000
  return points.map((point) => {
    const date = parseTime(point.bucket_start)
    if (stepMinutes <= 60) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
    return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  })
}

function render() {
  if (!chart) return
  const typography = chartTypography()
  const series = props.mode === 'tokens'
    ? [
        { name: '输入 Token', data: props.buckets.map((item) => item.input_tokens), color: '#3d8ff0' },
        { name: '输出 Token', data: props.buckets.map((item) => item.output_tokens), color: '#159d65' },
      ]
    : props.mode === 'value'
      ? [{ name: props.valueName, data: props.points.map((item) => item.value), color: '#0f6e4c' }]
      : props.mode === 'calls'
        ? [
            { name: '请求数', data: props.callPoints.map((item) => item.requests), color: '#159d65' },
            { name: '失败数', data: props.callPoints.map((item) => item.errors), color: '#e45d6a' },
          ]
        : [
            { name: '请求数', data: props.buckets.map((item) => item.requests), color: '#159d65' },
            { name: '成功', data: props.buckets.map((item) => item.success), color: '#3d8ff0' },
            { name: '失败', data: props.buckets.map((item) => item.errors), color: '#e45d6a' },
          ]
  chart.setOption({
    textStyle: typography,
    backgroundColor: 'transparent',
    grid: { top: 34, right: 14, bottom: 26, left: 52 },
    legend: { top: 0, right: 0, textStyle: { ...typography, color: '#3a524c' } },
    tooltip: { trigger: 'axis', backgroundColor: '#102522', borderColor: '#102522', textStyle: { ...typography, color: '#eaf6f1' } },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: axisLabels(),
      axisLine: { lineStyle: { color: '#d7e1dd' } },
      axisLabel: { ...typography, color: '#3a524c' },
    },
    yAxis: {
      type: 'value',
      splitNumber: 4,
      min: (extent: { min: number; max: number }) => (extent.max <= 0 && extent.min >= 0 ? 0 : extent.min),
      axisLabel: { ...typography, color: '#3a524c' },
      splitLine: { lineStyle: { color: '#e7eeeb' } },
    },
    series: series.map((item, index) => ({
      name: item.name,
      type: 'line',
      smooth: true,
      symbol: 'none',
      data: item.data,
      itemStyle: { color: item.color },
      lineStyle: { color: item.color, width: 2 },
      areaStyle: index === 0 ? { color: `${item.color}14` } : undefined,
    })),
  })
}

onMounted(async () => {
  await loadChartFonts()
  if (chartRef.value) {
    chart = echarts.init(chartRef.value)
    render()
  }
})
watch(() => [props.buckets, props.points, props.callPoints, props.mode, props.valueName], render, { deep: true })
useResizeObserver(chartRef, () => chart?.resize())
onBeforeUnmount(() => chart?.dispose())
</script>

<style scoped>
.gateway-trend { width: 100%; height: 200px; }
.gateway-trend--tall { height: 240px; }
.gateway-trend--compact { height: 180px; }
</style>