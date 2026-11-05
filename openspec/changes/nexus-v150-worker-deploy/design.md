## Decisions

复用现有受限 Nginx Worker 入口、远程 SSH、Gateway epoch 和 Worker 观测快照。平台 Skill 目录和版本是文件事实源。部署总截止条件由现有维护超时控制，验收不独立限制为 30 秒。认证错误直接反馈，已知瞬时 MCP 失败有限次重连。对既有安装仍遵守工作区和维护独占保护。

浏览器入口使用 HTTPS，Worker 入口使用受限 HTTP/WS。同机服务通过独立回环 HTTP 监听代理 Nexus API、AgentScope、Task 和 Schedule MCP。部署生成的客户端地址由同一 Worker HTTP 端口派生。
