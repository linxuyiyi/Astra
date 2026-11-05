## 设计

- 复用 Astra 页面骨架、Element Plus 表格及行操作菜单。任务和 Worker 列表保留数组响应并提供匹配总数，维护记录按服务端条件及页码查询。
- Worker 名称由任务读接口关联查询。原生工作区仍以实际 OpenCode 版本匹配构建包，恢复 iframe 启动所需的 Task Session 绑定。维护详情独立读取操作及顺序事件流。
- Nexus 保存自建 MCP 的配置和修订号，Agent Service 通过内部接口用官方 Storage 维护平台 owner 下的唯一 MCP 记录。员工和日程会话从同一平台库挂载。调度身份继续管理自己的会话。连接测试只挂载到校验工作区。
- 部署用同一个登记入口登记 Task 与 Schedule，保留这些服务的部署配置来源和业务授权。Library 保留 Worker OpenCode 的现有连接路径。
- 已产生的日程身份副本通过限定运行名与归属的一次性清理处理。
