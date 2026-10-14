# -*- coding: utf-8 -*-
"""按原生持久 workspace ID 建立物理隔离目录的工作区管理器。

官方 ``LocalWorkspaceManager``（AgentScope 2.0.10）虽按 ``workspace_id``
缓存，但把磁盘目录固定为 ``basedir/agent_id``，导致同一员工的不同 Session
实际共用同一目录。本模块只通过官方公开扩展点 ``WorkspaceManagerBase`` 与
``LocalWorkspace`` 修正目录选择：每个原生持久 ``workspace_id`` 映射到一个
稳定的哈希目录名，不同 Session 得到不同物理目录。

* 复用公开 ``assign_workspace_id``/``bind_storage``，不覆盖私有 ``_cache``；
* ``get_workspace`` 采用官方签名，使用调用方传入的原生持久 ``workspace_id``，
  仅在缺失时才按 ``PER_SESSION`` 策略分配；
* 目录名对 ``workspace_id`` 做 SHA-256，杜绝路径穿越；
* ``get_workspace``/``close``/``close_all`` 共用一把 manager 级 async 锁，
  覆盖完整的初始化与关闭生命周期，保证并发首次初始化只建一次实例，且
  ``close``/``close_all`` 不会在初始化中途返回、之后被迟到的初始化“复活”
  或产生同 ID 两个实例/孤儿资源（本版本无 default MCP，初始化轻量，单锁足够）；
* 不引入 TTL、引用计数、连接池或第二套资源层。
"""
from __future__ import annotations

import asyncio
import hashlib
import os

from agentscope.app.workspace_manager import (
    IsolationPolicy,
    WorkspaceManagerBase,
)
from agentscope.workspace import LocalWorkspace


def workspace_dir_name(workspace_id: str) -> str:
    """把原生 ``workspace_id`` 稳定编码为安全目录名。

    使用完整 SHA-256 十六进制摘要作为单一目录组件：既稳定（同一 ID 重启后
    映射同一目录），又不含 ``/``、``\\``、``..``，避免恶意或异常
    ``workspace_id`` 造成路径穿越。
    """
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()


class SessionWorkspaceManager(WorkspaceManagerBase):
    """按原生持久 ``workspace_id`` 分配独立物理目录的本地工作区管理器。

    事实源是原生 Session 持久化的 ``workspace_id``：``get_workspace`` 传入
    该 ID 时直接使用（缺失才按 ``PER_SESSION`` 分配），因此进程重启后同一
    workspace_id 重建同一目录并恢复已有文件，而不同 Session 的 ID 对应不同
    目录，互不共享文件。
    """

    def __init__(
        self,
        basedir: str,
        *,
        isolation: IsolationPolicy = IsolationPolicy.PER_SESSION,
        default_mcps: list | None = None,
        skill_paths: list[str] | None = None,
    ) -> None:
        """初始化管理器。

        Args:
            basedir (`str`):
                所有 workspace 目录的根目录。
            isolation (`IsolationPolicy`, defaults to `PER_SESSION`):
                ``assign_workspace_id`` 的分配粒度；本服务固定按 Session。
            default_mcps (`list | None`, optional):
                写入每个新建 workspace 的默认 MCP 声明。
            skill_paths (`list[str] | None`, optional):
                写入每个新建 workspace 的种子 Skill 目录。
        """
        self._basedir = os.path.abspath(basedir)
        self._default_mcps = default_mcps or []
        self._skill_paths = skill_paths or []
        # workspace_id -> 已初始化的 LocalWorkspace（本管理器自有缓存）。
        self._workspaces: dict[str, LocalWorkspace] = {}
        # 一把 manager 级生命周期锁：get/close/close_all 全程持锁。它同时
        # 串行并发首次初始化并让 close/close_all 等待同 workspace 的初始化
        # 结束，避免迟到发布与同 ID 双实例；初始化轻量（无 default MCP）。
        self._lock = asyncio.Lock()
        super().__init__(isolation=isolation)

    @property
    def basedir(self) -> str:
        """workspace 根目录（绝对路径）。"""
        return self._basedir

    def workdir_for(self, workspace_id: str) -> str:
        """返回某 ``workspace_id`` 的稳定物理目录（不创建、不初始化）。"""
        return os.path.join(self._basedir, workspace_dir_name(workspace_id))

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> LocalWorkspace:
        """返回已初始化的 workspace，按原生持久 ``workspace_id`` 分目录。

        并发首次请求同一 ``workspace_id`` 时只初始化一次并返回同一个实例：
        生命周期锁覆盖到 ``initialize`` 完成与缓存发布之后。``workspace_id``
        缺失时回退到公开 ``assign_workspace_id``（仅调用方没有持久绑定时发生）。
        """
        if not workspace_id:
            workspace_id = await self.assign_workspace_id(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            )

        async with self._lock:
            cached = self._workspaces.get(workspace_id)
            if cached is not None:
                return cached

            workspace = LocalWorkspace(
                workspace_id=workspace_id,
                workdir=self.workdir_for(workspace_id),
                default_mcps=self._default_mcps,
                skill_paths=self._skill_paths,
            )
            # 持锁初始化：close/close_all 与其他首次初始化都会等到这里结束，
            # 因此不会出现“close 返回后初始化再发布缓存”或同 ID 双实例。
            await workspace.initialize()
            self._workspaces[workspace_id] = workspace
            return workspace

    async def close(self, workspace_id: str) -> None:
        """关闭并移除单个缓存的 workspace。

        持生命周期锁：若有同 ID 初始化在途，会先等它结束再关闭，保证不
        复活、不漏关。
        """
        async with self._lock:
            workspace = self._workspaces.pop(workspace_id, None)
            if workspace is not None:
                await workspace.close()

    async def close_all(self) -> None:
        """关闭并清空所有缓存的 workspace。

        持生命周期锁：任何在途初始化结束后才执行，确保关闭后不会被迟到
        的初始化重新写入缓存。逐个在当前任务内关闭而非 ``gather``：stateful
        stdio MCP 的连接位于 ``anyio`` cancel scope，必须与关闭处于同一任务，
        否则跨任务退出 cancel scope 会报错并留下未关闭的进程。
        """
        async with self._lock:
            workspaces = list(self._workspaces.values())
            self._workspaces.clear()
            for workspace in workspaces:
                try:
                    await workspace.close()
                except Exception:  # noqa: BLE001 - 单个关闭失败不阻断其余
                    continue
