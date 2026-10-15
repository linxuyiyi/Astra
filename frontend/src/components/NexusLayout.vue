<script setup lang="ts">
// 公共 shell：顶部品牌、内容容器、底部三个导航位置。
// 数字员工与团队交流尚未接入：保持位置但明确 disabled，不跳转假页面。
</script>

<template>
  <div class="app">
    <header class="top">
      <div class="brand">
        <div class="mark" aria-hidden="true">✦</div>
        <div class="brandname">
          NEXUS <span class="version">v1.0.1</span>
        </div>
      </div>
    </header>

    <main class="page">
      <slot />
    </main>

    <nav class="dock" aria-label="页面导航">
      <button class="dock-btn active" type="button" aria-current="page">
        <span class="di" aria-hidden="true">◎</span>
        <span>员工管理</span>
      </button>
      <button
        class="dock-btn"
        type="button"
        disabled
        aria-disabled="true"
        title="数字员工页面尚未接入，后续阶段提供"
      >
        <span class="di" aria-hidden="true">◉</span>
        <span>数字员工</span>
      </button>
      <button
        class="dock-btn"
        type="button"
        disabled
        aria-disabled="true"
        title="团队交流页面尚未接入，后续阶段提供"
      >
        <span class="di" aria-hidden="true">☷</span>
        <span>团队交流</span>
      </button>
    </nav>
  </div>
</template>

<style scoped>
.app {
  position: relative;
  min-height: 100vh;
  padding: 21px 28px 105px;
  background:
    radial-gradient(ellipse 70% 50% at 82% 0, #e3f8f0, transparent 75%),
    radial-gradient(ellipse 60% 40% at 0 100%, #eaf4ff, transparent 75%),
    #f3f8f5;
}

.app::before {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(13, 101, 75, 0.022) 1px, transparent 1px),
    linear-gradient(90deg, rgba(13, 101, 75, 0.022) 1px, transparent 1px);
  background-size: 42px 42px;
  -webkit-mask-image: linear-gradient(to bottom, rgba(0, 0, 0, 0.22), transparent 85%);
  mask-image: linear-gradient(to bottom, rgba(0, 0, 0, 0.22), transparent 85%);
}

.top {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  height: 54px;
  max-width: 1560px;
  margin: auto;
}

.brand {
  display: flex;
  align-items: center;
  gap: 11px;
}

.mark {
  display: grid;
  place-items: center;
  width: 39px;
  height: 39px;
  border-radius: 12px;
  background: linear-gradient(135deg, #1ea87b, #066448);
  box-shadow: 0 8px 17px rgba(37, 142, 105, 0.18);
  color: #fff;
  font-size: 23px;
  font-weight: 700;
}

.brandname {
  font-size: 20px;
  font-weight: 850;
  letter-spacing: 2px;
}

.version {
  margin-left: 5px;
  color: #2d9c74;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.6px;
}

.page {
  position: relative;
  z-index: 1;
  max-width: 1560px;
  margin: 22px auto 0;
}

.dock {
  position: fixed;
  z-index: 20;
  bottom: 17px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px;
  border: 1px solid #d5e9dc;
  border-radius: 19px;
  background: rgba(250, 255, 251, 0.92);
  box-shadow: 0 13px 42px rgba(38, 113, 74, 0.145);
  backdrop-filter: blur(20px);
}

.dock-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  min-width: 89px;
  padding: 9px 17px;
  border: none;
  border-radius: 13px;
  background: transparent;
  color: #7a9b89;
  font-size: 10px;
  white-space: nowrap;
}

.dock-btn .di {
  font-size: 18px;
}

.dock-btn.active {
  background: #dff3e7;
  color: #147d59;
  font-weight: 750;
  cursor: default;
}

.dock-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

@media (max-width: 1000px) {
  .app {
    padding: 13px 14px 102px;
  }
}

@media (max-width: 650px) {
  .dock-btn {
    min-width: 78px;
    padding: 8px;
  }
}
</style>
