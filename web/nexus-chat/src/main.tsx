/**
 * Nexus 聊天岛入口。
 *
 * 只装配被复用的 AgentScope 聊天内核：ChatContent + useMessages + Markdown +
 * 工具渲染器。没有官方的侧栏、模型配置、巡览、任务面板。
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import "./nexus/theme.css";
import "./nexus/i18n";

import { IslandApp } from "./nexus/IslandApp";

const root = document.getElementById("root");
if (!root) throw new Error("缺少 #root 容器");

createRoot(root).render(
  <StrictMode>
    <IslandApp />
  </StrictMode>,
);
