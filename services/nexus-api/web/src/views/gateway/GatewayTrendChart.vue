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
  const theme = getComputedStyle(document.documentElement)
  const primary = theme.getPropertyValue('--astra-primary').trim()
  const neutral = theme.getPropertyValue('--astra-neutral').trim()
  const danger = theme.getPropertyValue('--astra-danger').trim()
  const muted = theme.getPropertyValue('--astra-text-muted').trim()
  const border = theme.getPropertyValue('--astra-border').trim()
  const series = props.mode === 'tokens'
    ? [
        { name: '输入 Token', data: props.buckets.map((item) => item.input_tokens), color: neutral },
        { name: '输出 Token', data: props.buckets.map((item) => item.output_tokens), color: primary },
      ]
    : props.mode === 'value'
      ? [{ name: props.valueName, data: props.points.map((item) => item.value), color: primary }]
      : props.mode === 'calls'
        ? [
            { name: '请求数', data: props.callPoints.map((item) => item.requests), color: primary },
            { name: '失败数', data: props.callPoints.map((item) => item.errors), color: danger },
          ]
        : [
            { name: '请求数', data: props.buckets.map((item) => item.requests), color: primary },
            { name: '成功', data: props.buckets.map((item) => item.success), color: neutral },
            { name: '失败', data: props.buckets.map((item) => item.errors), color: danger },
          ]
  chart.setOption({
    animation: false,
    textStyle: typography,
    backgroundColor: 'transparent',
    grid: { top: 34, right: 14, bottom: 26, left: 52 },
    legend: { top: 0, right: 0, textStyle: { ...typography, color: muted } },
    tooltip: { trigger: 'axis', backgroundColor: '#102522', borderColor: '#102522', textStyle: { ...typography, color: '#eaf6f1' } },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: axisLabels(),
      axisLine: { lineStyle: { color: border } },
      axisLabel: { ...typography, color: muted },
    },
    yAxis: {
      type: 'value',
      splitNumber: 4,
      min: (extent: { min: number; max: number }) => (extent.max <= 0 && extent.min >= 0 ? 0 : extent.min),
      axisLabel: { ...typography, color: muted },
      splitLine: { lineStyle: { color: border } },
    },
    series: series.map((item) => ({
      name: item.name,
      type: 'line',
      smooth: 0.25,
      symbol: 'none',
      data: item.data,
      itemStyle: { color: item.color },
      lineStyle: { color: item.color, width: 2 },
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