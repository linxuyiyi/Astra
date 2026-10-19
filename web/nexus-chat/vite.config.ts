import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const src = fileURLToPath(new URL("./src", import.meta.url));

// 上游 AgentScope chat 组件通过 `@/...` 引用自己的 src 根。这里把 4 个「环境模块」
// 改指向 shims：它们依赖 AgentScope 的 /sessions、/chat、workspace 接口与浏览器语音，
// 都由 Nexus 侧替代。其余 `@/` 路径与上游完全一致，vendored 源码保持逐字节不变。
export default defineConfig({
  base: "/nexus-chat/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      { find: /^@\/api$/, replacement: `${src}/shims/api.ts` },
      { find: /^@\/context\/AudioContext$/, replacement: `${src}/shims/AudioContext.tsx` },
      {
        find: /^@\/components\/dialog\/WorkingDirectoryDialog$/,
        replacement: `${src}/shims/WorkingDirectoryDialog.tsx`,
      },
      {
        find: /^@\/lib\/browserWebRTCTransport$/,
        replacement: `${src}/shims/browserWebRTCTransport.ts`,
      },
      { find: /^@\//, replacement: `${src}/` },
    ],
  },
  build: {
    outDir: "../nexus/public/nexus-chat",
    emptyOutDir: true,
    sourcemap: false,
  },
});
