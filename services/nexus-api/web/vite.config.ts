import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, URL } from "node:url";
import { defineConfig, type Plugin } from "vite";
import vue from "@vitejs/plugin-vue";

const root = fileURLToPath(new URL(".", import.meta.url));

function missingWorkspaceBundle(): Plugin {
  const reject = (req: { url?: string }, res: { statusCode: number; setHeader: (name: string, value: string) => void; end: (body: string) => void }, next: () => void) => {
    const url = (req.url ?? "").split("?")[0];
    if (!url.startsWith("/opencode-workspace/")) return next();
    const file = path.join(root, "public", decodeURIComponent(url));
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.statusCode = 404;
      res.end("OpenCode Workspace UI bundle is not installed");
      return;
    }
    // index.html points at the hashed bundle. Keep it revalidatable; cache the assets.
    res.setHeader(
      "Cache-Control",
      url.endsWith("/index.html") ? "no-cache" : "public, max-age=31536000, immutable",
    );
    next();
  };
  return {
    name: "opencode-workspace-missing",
    configureServer(server) {
      server.middlewares.use(reject);
    },
    configurePreviewServer(server) {
      server.middlewares.use(reject);
    },
  };
}

// 官方 AgentScope Web UI 以 history 路由挂在 /agentscope-ui/。静态资源直接命中 public，
// 其余无扩展名路径回退到官方 index.html，避免被 Nexus 自己的 SPA 兜底。
function agentscopeUiSpa(): Plugin {
  const rewrite = (req: { url?: string }) => {
    const url = (req.url ?? "").split("?")[0];
    if (url === "/agentscope-ui" || url === "/agentscope-ui/") {
      req.url = "/agentscope-ui/index.html";
      return;
    }
    if (!url.startsWith("/agentscope-ui/")) return;
    if (url.startsWith("/agentscope-ui/assets/") || path.extname(url)) return;
    req.url = "/agentscope-ui/index.html";
  };
  return {
    name: "agentscope-ui-spa",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        rewrite(req);
        next();
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use((req, _res, next) => {
        rewrite(req);
        next();
      });
    },
  };
}

const proxy = {
  // Local dev only: route ASR calls to the standalone ASR server. Production
  // uses the Nginx location from services/nexus-asr/deploy/nginx-nexus-asr.conf.
  "/api/asr": {
    target: process.env.VITE_NEXUS_ASR_PROXY_TARGET ?? "http://127.0.0.1:19899",
    changeOrigin: false,
  },
  "/api/v1": process.env.VITE_NEXUS_API_PROXY_TARGET ?? "http://127.0.0.1:18120",
  "/api": process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:18120",
  "/v1": process.env.VITE_GATEWAY_PROXY_TARGET ?? "http://127.0.0.1:18110",
  "/agentscope-api": {
    target: process.env.VITE_AGENTSCOPE_PROXY_TARGET ?? "http://127.0.0.1:18000",
    changeOrigin: false,
    rewrite: (requestPath: string) => requestPath.replace(/^\/agentscope-api/, ""),
  },
};

export default defineConfig({
  plugins: [vue(), missingWorkspaceBundle(), agentscopeUiSpa()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 15174,
    proxy,
  },
  preview: {
    proxy,
  },
});
