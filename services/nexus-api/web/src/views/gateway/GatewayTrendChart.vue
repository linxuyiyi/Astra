<template>
  <div ref="chartRef" class="gateway-trend" :class="{ 'gateway-trend--tall': tall, 'gateway-trend--compact': compact }" role="img" :aria-label="ariaLabel" />
</template>

<script setup lang="ts">
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
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
    mode?: 'tokens' | 'requests' | 'value' | 'calls' | 'total-tokens'
    valueName?: string
    unit?: string
    tall?: boolean
    compact?: boolean
  }>(),
  {
    buckets: () => [],
    points: () => [],
    callPoints: () => [],
    mode: 'tokens',
    valueName: '数值',
    unit: '',
    tall: false,
    compact: false,
  },
)

const chartRef = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const ariaLabel = computed(() => {
  if (props.mode === 'tokens' || props.mode === 'total-tokens') return 'API 网关 Token 使用趋势图'
  if (props.mode === 'value') return `API 网关 ${props.valueName} 趋势图`
  if (props.mode === 'calls') return 'API 网关请求与失败趋势图'
  return 'API 网关请求量趋势图'
})

function parseTime(value: string) {
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`)
}

function axisLabels() {
  const source = props.mode === 'value' || props.mode === 'total-tokens'
    ? props.points
    : props.mode === 'calls'
      ? props.callPoints
      : props.buckets
  if (source.length < 2) {
    return source.map((point) => parseTime(point.bucket_start).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }))
  }
  const stepMinutes = (parseTime(source[1].bucket_start).getTime() - parseTime(source[0].bucket_start).getTime()) / 60_000
  return source.map((point) => {
    const date = parseTime(point.bucket_start)
    if (stepMinutes <= 60) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
    return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  })
}

function compactAxisValue(val: number): string {
  if (val >= 1_000_000) return `${(val / 1_000_000).toFixed(val % 1_000_000 === 0 ? 0 : 1)}M`
  if (val >= 1_000) return `${(val / 1_000).toFixed(val % 1_000 === 0 ? 0 : 1)}K`
  return props.unit ? `${val}${props.unit}` : String(val)
}

function render() {
  if (!chart) return
  const typography = chartTypography()
  const theme = getComputedStyle(document.documentElement)
  const primary = theme.getPropertyValue('--astra-primary').trim() || '#18181b'
  const blue = theme.getPropertyValue('--astra-info').trim() || '#2563eb'
  const danger = theme.getPropertyValue('--astra-danger').trim() || '#dc2626'
  const muted = theme.getPropertyValue('--astra-text-muted').trim() || '#71717a'
  const border = theme.getPropertyValue('--astra-border').trim() || '#e4e4e7'
  const subtleBorder = theme.getPropertyValue('--astra-border-subtle').trim() || '#f0f0f3'

  const isBarLike = props.mode === 'calls' || props.mode === 'tokens' || props.mode === 'total-tokens'
  let series: Record<string, unknown>[] = []

  if (props.mode === 'calls') {
    series = [
      {
        name: '成功请求',
        type: 'bar',
        stack: 'req',
        barMaxWidth: 18,
        data: props.callPoints.map((item) => Math.max(0, item.requests - item.errors)),
        itemStyle: { color: '#27272a', borderRadius: [3, 3, 0, 0] },
      },
      {
        name: '失败请求',
        type: 'bar',
        stack: 'req',
        barMaxWidth: 18,
        data: props.callPoints.map((item) => item.errors),
        itemStyle: { color: danger, borderRadius: [3, 3, 0, 0] },
      },
    ]
  } else if (props.mode === 'tokens') {
    series = [
      {
        name: '输入 Token',
        type: 'bar',
        stack: 'tok',
        barMaxWidth: 18,
        data: props.buckets.map((item) => item.input_tokens),
        itemStyle: { color: blue, borderRadius: [0, 0, 0, 0] },
      },
      {
        name: '输出 Token',
        type: 'bar',
        stack: 'tok',
        barMaxWidth: 18,
        data: props.buckets.map((item) => item.output_tokens),
        itemStyle: { color: '#3f3f46', borderRadius: [3, 3, 0, 0] },
      },
    ]
  } else if (props.mode === 'total-tokens') {
    series = [
      {
        name: props.valueName || '总 Token',
        type: 'bar',
        barMaxWidth: 18,
        data: props.points.map((item) => item.value ?? 0),
        itemStyle: { color: blue, borderRadius: [3, 3, 0, 0] },
      },
    ]
  } else if (props.mode === 'value') {
    const validCount = props.points.filter((item) => item.value !== null && item.value !== undefined).length
    series = [
      {
        name: props.valueName,
        type: 'line',
        smooth: 0.3,
        connectNulls: true,
        symbol: 'circle',
        symbolSize: validCount <= 3 ? 7 : 5,
        showSymbol: validCount <= 3,
        data: props.points.map((item) => item.value),
        itemStyle: { color: primary },
        lineStyle: { color: primary, width: 2 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(24, 24, 27, 0.14)' },
            { offset: 1, color: 'rgba(24, 24, 27, 0.01)' },
          ]),
        },
      },
    ]
  } else {
    series = [
      {
        name: '请求数',
        type: 'line',
        smooth: 0.25,
        showSymbol: false,
        data: props.buckets.map((item) => item.requests),
        itemStyle: { color: primary },
        lineStyle: { color: primary, width: 2 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(24, 24, 27, 0.10)' },
            { offset: 1, color: 'rgba(24, 24, 27, 0.0)' },
          ]),
        },
      },
      {
        name: '成功',
        type: 'line',
        smooth: 0.25,
        showSymbol: false,
        data: props.buckets.map((item) => item.success),
        itemStyle: { color: blue },
        lineStyle: { color: blue, width: 1.75 },
      },
      {
        name: '失败',
        type: 'line',
        smooth: 0.25,
        showSymbol: false,
        data: props.buckets.map((item) => item.errors),
        itemStyle: { color: danger },
        lineStyle: { color: danger, width: 1.75 },
      },
    ]
  }

  chart.setOption({
    animation: false,
    textStyle: typography,
    backgroundColor: 'transparent',
    grid: { top: 32, right: 12, bottom: 24, left: 48, containLabel: false },
    legend: {
      top: 0,
      right: 0,
      itemWidth: 10,
      itemHeight: 8,
      icon: isBarLike ? 'roundRect' : 'circle',
      textStyle: { ...typography, color: muted, fontSize: 12 },
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: isBarLike ? 'shadow' : 'line',
        shadowStyle: { color: 'rgba(24, 24, 27, 0.04)' },
        lineStyle: { color: '#a1a1aa', type: 'dashed' },
      },
      backgroundColor: '#18181b',
      borderColor: '#27272a',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { ...typography, color: '#fafafa', fontSize: 12 },
    },
    xAxis: {
      type: 'category',
      boundaryGap: isBarLike,
      data: axisLabels(),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: border } },
      axisLabel: { ...typography, color: muted, fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      splitNumber: 4,
      min: 0,
      axisLabel: {
        ...typography,
        color: muted,
        fontSize: 11,
        formatter: (value: number) => compactAxisValue(value),
      },
      splitLine: { lineStyle: { color: subtleBorder, type: 'dashed' } },
    },
    series,
  }, true)
}

onMounted(async () => {
  await loadChartFonts()
  if (chartRef.value) {
    chart = echarts.init(chartRef.value)
    render()
  }
})
watch(() => [props.buckets, props.points, props.callPoints, props.mode, props.valueName, props.unit], render, { deep: true })
useResizeObserver(chartRef, () => chart?.resize())
onBeforeUnmount(() => chart?.dispose())
</script>

<style scoped>
.gateway-trend { width: 100%; height: 210px; }
.gateway-trend--tall { height: 248px; }
.gateway-trend--compact { height: 180px; }
@media (min-width: 2240px) and (min-height: 1180px) {
  .gateway-trend { height: 260px; }
  .gateway-trend--tall { height: 320px; }
  .gateway-trend--compact { height: 220px; }
}
</style>
