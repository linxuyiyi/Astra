import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 宿主页按 manifest 加载带内容哈希的模块与样式，聊天区按需下载。
export default defineConfig({
  base: "/nexus-chat/",
  plugins: [react()],
  build: {
    outDir: "../web/public/nexus-chat",
    emptyOutDir: true,
    manifest: "manifest.json",
    rollupOptions: {
      input: fileURLToPath(new URL("./src/mount.tsx", import.meta.url)),
      preserveEntrySignatures: "strict",
    },
  },
});
