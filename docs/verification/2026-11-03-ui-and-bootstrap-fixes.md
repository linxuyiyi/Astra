# 2026-11-03 UI 与部署修复验证

## 原因与修改

- 技能管理页重复显示 AgentScope 连接状态：移除页面底部的重复项，保留全局连接状态。
- 分页底栏继承页面绿色背景：统一使用白色卡片背景，保留 sticky 时遮挡下方表格内容的能力。
- 列表卡片与表格同时限制高度：移除列表表格的 viewport max-height，只由卡片 body 滚动；配置页滚动样式限定为直接子卡片 body，避免影响嵌套卡片。弹窗、详情的固定高度不变。
- npm 配置检查会因已有 registry 或 strict-ssl=false 中断部署：删除这些阻断校验，保留已有 npmrc；没有 registry 时补充部署默认值。本次没有修改 pip 校验，也不改写 strict-ssl。
- 无可用解释器时，端口监听一律中止：使用 Linux /proc 确认目标端口对应 PID、可执行文件和 serve 参数；识别 OpenCode 后发送 TERM，等待端口释放再继续。其他监听进程不停止；停止失败仍报错。支持实际 Linux 安装包中的 opencode.exe 文件名。

## 验证

- 后端定向回归：72 passed（包来源、部署包、远程引导、远程部署）。
- 前端：79 tests passed，类型检查和生产构建通过；存在原有大 bundle 提示。
- Ruff 与 git diff --check 通过。
- Lima Linux 隔离验证：真实 /usr/local/bin/opencode serve 使用临时 HOME、临时工作目录和随机本地端口启动；实际清理脚本返回 STOPPED，进程退出 -15。未操作现有 Worker/OpenCode 服务。
- Linux 进程边界：受控 Node OpenCode 包装进程返回 STOPPED；其他监听程序返回 FOREIGN 且保持运行；释放后的空端口返回 CLEAR。临时进程全部清理。
- 桌面浏览器 1440×900：最新构建预览中技能页没有重复连接状态；分页背景为 rgb(255,255,255)。20 行任务响应使用临时预览 fixture，唯一纵向滚动容器为 el-card__body，滚动至第 20 行可见；页面本身不额外滚动。预览仅代理现有 API 的 GET，未写入业务数据。

## 验证边界

未在公司目标机器执行完整的一键部署。本地已有服务与数据库未重启或升级；页面响应中的旧业务数据不代表最新后端行为。
