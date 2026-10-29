/** 本地增量验收：既有 API 走运行中的 Nexus，新增只读办公室接口走待验版本。
 * 不修改线上进程或配置，不启动第二个调度器。
 * LIVE_API=http://127.0.0.1:18520 OFFICE_API=http://127.0.0.1:18549 node tools/e2e/nexus_pixel_office_live_preview.mjs
 */
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const webRoot = path.join(root, "services/nexus-api/web");
const require = createRequire(path.join(webRoot, "package.json"));
const { createServer, loadConfigFromFile } = await import(
  pathToFileURL(require.resolve("vite")).href
);
const live = process.env.LIVE_API || "http://127.0.0.1:18520";
const office = process.env.OFFICE_API || "http://127.0.0.1:18549";
const loaded = await loadConfigFromFile(
  { command: "serve", mode: "development" },
  path.join(webRoot, "vite.config.ts"),
);
const config = loaded.config;
config.configFile = false;
config.root = webRoot;
config.server = {
  ...config.server,
  host: "127.0.0.1",
  port: Number(process.env.PORT || 15149),
  strictPort: true,
  proxy: {
    "^/api/v1/nebula/employees/[^/]+/office": { target: office },
    "/api/v1": live,
    "/api": live,
    "/agentscope-api": live,
    "/v1": config.server.proxy["/v1"],
  },
};
const server = await createServer(config);
await server.listen();
server.printUrls();
