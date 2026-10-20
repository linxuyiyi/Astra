/** Canvas text cannot inherit CSS; read the same tokens used by DOM components. */
export function chartTypography() {
  const style = getComputedStyle(document.documentElement)
  return {
    fontFamily: style.getPropertyValue('--astra-font-family').trim(),
    fontSize: Number.parseFloat(style.getPropertyValue('--astra-font-helper')),
    fontWeight: Number.parseInt(style.getPropertyValue('--astra-weight-regular'), 10),
  }
}

/** Wait before the first Canvas draw so a locally resolved face is not cached as fallback. */
export async function loadChartFonts() {
  const { fontFamily, fontSize, fontWeight } = chartTypography()
  await Promise.allSettled([
    document.fonts.load(`${fontWeight} ${fontSize}px ${fontFamily}`, 'English 0123456789 输入输出今日'),
  ])
}
