# Nexus 前端（第一阶段 3.3.1）

Vue 3 + TypeScript + Element Plus + Vite，当前只提供员工管理基础页面：
员工列表/服务端搜索、员工档案、创建编辑抽屉、启停、三类真实资源选择。
删除、私聊与团队交流尚未接入（底部导航与其他入口明确禁用）。

## 环境要求

Node.js `>=24`（`npm test` 依赖 Node 原生 TypeScript 类型剥离运行 `.ts` 测试）。

## 安装与命令

```bash
cd frontend
npm install
npm run dev          # 开发服务器
npm run type-check   # vue-tsc --noEmit
npm run build        # 类型检查 + vite build
npm run test         # node --test（纯逻辑行为测试）
```

## 后端代理

浏览器只访问 Nexus 后端的同源 `/api`；开发期由 Vite 代理到真实后端，
默认 `http://127.0.0.1:8765`（与仓库后端默认端口一致）。

需要指向隔离的真实联调后端时，用服务端环境变量覆盖（不会下发到浏览器）：

```bash
NEXUS_BACKEND_URL=http://127.0.0.1:9000 npm run dev
```

也可在 `frontend/.env.local` 写入 `NEXUS_BACKEND_URL=...`（已忽略提交）。
前端不接触 AgentScope 地址、模型密钥或任何内部 token，也不引入宽泛 CORS。

## 范围说明

- 业务数据以 Nexus 后端/AgentScope 为准，前端只保留选中 ID、草稿与 UI 状态。
- 三类资源（知识/技能/工具）来自真实目录，只有 `enabled && selectable`
  才可新增选择；已保存但失效的项保留并标注，不因目录故障静默清空。
- `tools` 对应后端资源目录的 `mcps`。
