import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

// 浏览器只访问 Nexus 后端同源 /api；开发期由 Vite 代理到真实后端。
// NEXUS_BACKEND_URL 只在 Node（服务端）读取，不下发到客户端，也不暴露
// AgentScope 地址、模型密钥或内部 token。默认与本仓库后端 127.0.0.1:8765 一致。
export default defineConfig(({ mode }) => {
  const fileEnv = loadEnv(mode, process.cwd(), 'NEXUS_')
  const backendUrl =
    process.env.NEXUS_BACKEND_URL ||
    fileEnv.NEXUS_BACKEND_URL ||
    'http://127.0.0.1:8765'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      proxy: {
        '/api': {
          target: backendUrl,
          changeOrigin: false,
        },
      },
    },
  }
})
