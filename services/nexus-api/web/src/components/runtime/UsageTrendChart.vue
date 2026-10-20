<template><div ref="chartRef" class="usage-trend-chart" role="img" aria-label="Token 使用趋势图" /></template>

<script setup lang="ts">
import * as echarts from 'echarts/core'
import { chartTypography, loadChartFonts } from '@/utils/typography'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useResizeObserver } from '@vueuse/core'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { UsageSummary } from '@/api/types'

const props = defineProps<{ data: Array<{ date: string } & UsageSummary> }>()
const chartRef = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

function render() {
  if (!chart) return
  const typography = chartTypography()
  chart.setOption({
    textStyle: typography,
    backgroundColor: 'transparent',
    grid: { top: 34, right: 14, bottom: 24, left: 48 },
    legend: { top: 0, right: 0, textStyle: { ...typography, color: '#617771', fontSize: typography.fontSize } },
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d7e1dd', textStyle: { ...typography, color: '#102522' } },
    xAxis: { type: 'category', boundaryGap: false, data: props.data.map((item) => item.date.slice(5)), axisLine: { lineStyle: { color: '#d7e1dd' } }, axisLabel: { ...typography, color: '#617771', fontSize: typography.fontSize } },
    yAxis: { type: 'value', axisLabel: { ...typography, color: '#617771', fontSize: typography.fontSize }, splitLine: { lineStyle: { color: '#e3eae7' } } },
    series: [
      { name: '输入', type: 'line', smooth: true, symbol: 'none', data: props.data.map((item) => item.input_tokens ?? 0), lineStyle: { color: '#159d65', width: 2 }, areaStyle: { color: 'rgba(21,157,101,.08)' } },
      { name: '输出', type: 'line', smooth: true, symbol: 'none', data: props.data.map((item) => item.output_tokens ?? 0), lineStyle: { color: '#7658d9', width: 2 } },
      { name: '缓存读取', type: 'line', smooth: true, symbol: 'none', data: props.data.map((item) => item.cache_read_tokens ?? 0), lineStyle: { color: '#e9953f', width: 1.5 } },
    ],
  })
}

onMounted(async () => {
  await loadChartFonts()
 if (chartRef.value) { chart = echarts.init(chartRef.value); render() } })
watch(() => props.data, render, { deep: true })
useResizeObserver(chartRef, () => chart?.resize())
onBeforeUnmount(() => chart?.dispose())
</script>
