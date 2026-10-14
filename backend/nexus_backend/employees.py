# -*- coding: utf-8 -*-
"""员工管理业务（OpenSpec 任务 3.1）。

职责边界：

* 姓名与实际执行 prompt 以 AgentScope 原生 ``AgentRecord`` 为事实源；真实
  ``POST /agent/``、``PATCH /agent/{id}``，绑定始终使用原生顶层 id。
* Nexus 只保存必要业务扩展（领域、职责、工作指令、头像、启用、资源默认
  关联占位）与忽略大小写的唯一性索引，不建立第二套 Agent 配置。
* 职责与工作指令编译成明确中文 ``system_prompt`` 后写入原生 Agent；编译时
  附带服务端生成的稳定员工 ID 作为严格关联标记，仅用于结果未知时核对真实
  原生记录，不按名称近似、时间戳或事件数量猜测。

跨服务写入的最小写入意图（不假称原子，也不引入分布式事务框架）：

* 创建：先短事务落库带稳定员工 ID 的非 ready 占位（名称/领域唯一约束兜底
  并发重复），再进行原生 ``POST``。已知成功读取原生确认后才绑定顶层 id 并
  ready；超时/连接失败/5xx/异常 2xx 等结果未知时一律保留占位与意图并返回可
  核对的员工 ID，不自动重发非幂等 POST；仅明确未受理的 4xx 才删除占位让用户
  重试；清理失败如实报告未完成而非宣称已清理。
* 编辑：网络调用前先短事务持久化非 ready、写入意图与**真实原生旧值快照**，再
  ``PATCH`` 原生。成功后短事务落本地并清除意图恢复 ready；未知结果保持未完成，
  ``reconcile`` 对相同目标幂等重试并核对 desired 后才恢复 ready。仅凭读取到旧
  值**不能**判定未生效（原请求可能稍后提交），故只有明确未受理的 4xx 才允许在
  精确旧快照核对一致后恢复 ready。本地保存失败不回滚原生而是保留意图。
* reconcile：核对原生记录与写入意图。原生与意图一致则落本地并恢复 ready；未知
  结果对相同目标幂等重试、确认 desired 后才 ready；无法判定则保持 pending/
  incomplete 并禁止新运行。创建恢复绑定只认编译 prompt 末尾规范的稳定员工 ID，
  且绑定前必须核对原生 name/prompt 与创建意图完全一致，否则标记 incomplete；
  创建占位仅在确认原生从未创建时才允许释放，且核对与释放必须整个在同一员工锁内。
* 同一员工（含创建后的绑定与恢复）的写操作在单进程每员工 ``asyncio.Lock`` 内
  串行；该异步锁**覆盖原生网络调用序列**，只有数据库事务保持短小、不在其中做
  网络 I/O。

启停执行约束、资源真实绑定与 DELETE 归档属于任务 3.2，本模块不提前注册
假成功接口。
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .adapter import AgentScopeHTTPClient, UpstreamError
from .db import Database, Employee
from . import resources

# 原型固定的五个员工形象（字符串规范枚举）。
AVATARS: tuple[str, ...] = (
    "👩🏻‍💻",  # 工程师
    "🧑🏻‍🔬",  # 分析师
    "👩🏽‍🚀",  # 技术专家
    "🤖",  # 数字员工
    "🧑🏽‍💻",  # 工程师 B
)
DEFAULT_AVATAR = AVATARS[0]
NAME_MAX_LENGTH = 24
DOMAIN_MAX_LENGTH = 42

# 稳定关联标记：编译进原生 system_prompt，仅用于核对本次创建/编辑对应的
# 真实原生记录，不依赖名称近似或时间戳。
MARKER_PREFIX = "[nexus-employee-id:"
MARKER_SUFFIX = "]"
# 独立严格修订标记：与稳定员工 ID 标记相邻，供服务读屏障校验单调写入修订。
# 必须与 agent-service ``write_barrier`` 的同名常量保持一致。
REVISION_PREFIX = "[nexus-write-revision:"
REVISION_SUFFIX = "]"

SYNC_READY = "ready"
SYNC_CREATING = "creating"
SYNC_UPDATING = "updating"
SYNC_INCOMPLETE = "incomplete"

# 创建是员工的第一次原生写入，修订从 1 开始单调递增。
CREATE_REVISION = 1

_INTENT_FIELDS = (
    "name",
    "domain",
    "responsibilities",
    "instructions",
    "avatar",
    "enabled",
    "knowledge",
    "skills",
    "tools",
)

RESOURCE_FIELDS = ("knowledge", "skills", "tools")
RESOURCE_MAX = 50


class EmployeeCreate(BaseModel):
    """创建员工请求体（业务校验在服务端显式执行）。

    支持业务字段、``enabled`` 启停与 Knowledge/Skill/MCP Tools 默认关联
    （列表仅含稳定资源 ID）。未知字段一律显式拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    domain: str | None = None
    responsibilities: str | None = None
    instructions: str | None = None
    avatar: str | None = None
    enabled: bool | None = None
    knowledge: list[str] | None = None
    skills: list[str] | None = None
    tools: list[str] | None = None


class EmployeeUpdate(BaseModel):
    """编辑员工请求体。

    只接受已实现的员工字段；``enabled``、``knowledge``/``skills``/``tools``
    默认关联均为正式字段，未知字段一律显式拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    domain: str | None = None
    responsibilities: str | None = None
    instructions: str | None = None
    avatar: str | None = None
    enabled: bool | None = None
    knowledge: list[str] | None = None
    skills: list[str] | None = None
    tools: list[str] | None = None


class ReconcileRequest(BaseModel):
    """reconcile 请求体：仅创建占位确认“原生从未创建”时才允许释放。"""

    model_config = ConfigDict(extra="forbid")

    confirm_not_created: bool = False


class EmployeeValidationError(ValueError):
    """业务字段校验失败，携带逐字段中文错误。"""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("；".join(f"{k}: {v}" for k, v in errors.items()))


def normalize(value: str) -> str:
    """忽略大小写的唯一性归一化键。"""
    return value.casefold()


def employee_marker(employee_id: str) -> str:
    """稳定关联标记字符串。"""
    return f"{MARKER_PREFIX}{employee_id}{MARKER_SUFFIX}"


def revision_marker(revision: int) -> str:
    """单调写入修订标记字符串（必须与原生服务解析规则一致）。"""
    return f"{REVISION_PREFIX}{revision}{REVISION_SUFFIX}"


def _exact_marker_line(line: str, prefix: str) -> str | None:
    """整行恰为 ``prefix...]`` 标记时返回内部值，否则 ``None``。"""
    if not line.startswith(prefix) or not line.endswith(REVISION_SUFFIX):
        return None
    value = line[len(prefix) : -len(REVISION_SUFFIX)]
    return value or None


def parse_prompt_markers(prompt: str | None) -> tuple[str | None, int | None]:
    """只接受编译 prompt 末尾规范的“员工 ID 标记行 + 修订标记最后一行”对。

    与 agent-service ``write_barrier.parse_managed_markers`` 保持一致：普通
    职责/指令正文即使含相同标记文本也不参与解析，避免正文误命中导致修订被
    误解析。
    """
    if not isinstance(prompt, str):
        return None, None
    trimmed = prompt.rstrip()
    if not trimmed:
        return None, None
    lines = trimmed.split("\n")
    if len(lines) < 2:
        return None, None
    employee_id = _exact_marker_line(lines[-2].strip(), MARKER_PREFIX)
    revision_raw = _exact_marker_line(lines[-1].strip(), REVISION_PREFIX)
    if employee_id is None or revision_raw is None or not revision_raw.isdigit():
        return None, None
    return employee_id, int(revision_raw)


def parse_prompt_revision(prompt: str | None) -> int | None:
    """从规范末尾标记解析已有修订；缺失或非法返回 ``None``。"""
    _, revision = parse_prompt_markers(prompt)
    return revision


def compile_system_prompt(
    *,
    employee_id: str,
    name: str,
    domain: str,
    responsibilities: str,
    instructions: str,
    revision: int = 0,
) -> str:
    """把员工业务字段编译成明确中文执行 prompt，并附带稳定关联标记。

    ``revision`` 为本次写入的单调修订号，附在稳定员工 ID 标记旁，供原生
    服务写屏障拒绝迟到的旧写请求。
    """
    return (
        f"你是 Nexus 数字员工「{name}」，责任领域：{domain}。\n\n"
        f"你的职责说明如下：\n{responsibilities}\n\n"
        f"你必须遵守以下工作指令：\n{instructions}\n\n"
        f"{employee_marker(employee_id)}\n"
        f"{revision_marker(revision)}"
    )


def new_employee_id() -> str:
    return f"emp_{uuid.uuid4().hex}"


def _required_text(
    value: object,
    field: str,
    errors: dict[str, str],
    *,
    max_length: int | None = None,
) -> str | None:
    if value is None:
        errors[field] = "必填"
        return None
    if not isinstance(value, str):
        errors[field] = "必须是文本"
        return None
    text = value.strip()
    if not text:
        errors[field] = "必填"
        return None
    if max_length is not None and len(text) > max_length:
        errors[field] = f"最多 {max_length} 个字符"
        return None
    return text


def _validated_avatar(value: object, errors: dict[str, str]) -> str | None:
    if value is None or value == "":
        return DEFAULT_AVATAR
    if not isinstance(value, str) or value not in AVATARS:
        errors["avatar"] = "必须从预设员工形象中选择"
        return None
    return value


def _validated_id_list(
    value: object,
    field: str,
    errors: dict[str, str],
) -> list[str] | None:
    """校验默认关联资源 ID 列表：仅接受稳定 ID 字符串，去重保序。

    ``None`` 表示字段未提供（创建时按空列表处理）。资源是否真实存在且
    enabled 由后续目录核对负责。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        errors[field] = "必须是资源 ID 列表"
        return None
    result: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            errors[field] = "资源 ID 必须是非空文本"
            return None
        identifier = entry.strip()
        if identifier in seen:
            continue
        seen.add(identifier)
        result.append(identifier)
    if len(result) > RESOURCE_MAX:
        errors[field] = f"最多关联 {RESOURCE_MAX} 个资源"
        return None
    return result


def validate_create(payload: EmployeeCreate) -> dict:
    errors: dict[str, str] = {}
    name = _required_text(
        payload.name,
        "name",
        errors,
        max_length=NAME_MAX_LENGTH,
    )
    domain = _required_text(
        payload.domain,
        "domain",
        errors,
        max_length=DOMAIN_MAX_LENGTH,
    )
    responsibilities = _required_text(
        payload.responsibilities,
        "responsibilities",
        errors,
    )
    instructions = _required_text(payload.instructions, "instructions", errors)
    avatar = _validated_avatar(payload.avatar, errors)
    knowledge = _validated_id_list(payload.knowledge, "knowledge", errors)
    skills = _validated_id_list(payload.skills, "skills", errors)
    tools = _validated_id_list(payload.tools, "tools", errors)
    if errors:
        raise EmployeeValidationError(errors)
    return {
        "name": name,
        "domain": domain,
        "responsibilities": responsibilities,
        "instructions": instructions,
        "avatar": avatar,
        "enabled": True if payload.enabled is None else payload.enabled,
        "knowledge": knowledge,
        "skills": skills,
        "tools": tools,
    }


def validate_update(payload: EmployeeUpdate) -> dict:
    """只校验请求中实际提供的字段，返回待更新字段。"""
    provided = payload.model_dump(exclude_unset=True)
    errors: dict[str, str] = {}
    updates: dict = {}
    if "name" in provided:
        name = _required_text(
            provided["name"],
            "name",
            errors,
            max_length=NAME_MAX_LENGTH,
        )
        if name is not None:
            updates["name"] = name
    if "domain" in provided:
        domain = _required_text(
            provided["domain"],
            "domain",
            errors,
            max_length=DOMAIN_MAX_LENGTH,
        )
        if domain is not None:
            updates["domain"] = domain
    if "responsibilities" in provided:
        responsibilities = _required_text(
            provided["responsibilities"],
            "responsibilities",
            errors,
        )
        if responsibilities is not None:
            updates["responsibilities"] = responsibilities
    if "instructions" in provided:
        instructions = _required_text(
            provided["instructions"],
            "instructions",
            errors,
        )
        if instructions is not None:
            updates["instructions"] = instructions
    if "avatar" in provided:
        avatar = _validated_avatar(provided["avatar"], errors)
        if avatar is not None:
            updates["avatar"] = avatar
    if "enabled" in provided:
        enabled = provided["enabled"]
        if not isinstance(enabled, bool):
            errors["enabled"] = "必须是布尔值"
        else:
            updates["enabled"] = enabled
    for field in RESOURCE_FIELDS:
        if field in provided:
            parsed = _validated_id_list(provided[field], field, errors)
            if parsed is not None:
                updates[field] = parsed
    if errors:
        raise EmployeeValidationError(errors)
    return updates


async def _validate_selected_resources(
    adapter: AgentScopeHTTPClient,
    updates: dict,
) -> dict:
    """核对默认关联资源真实存在且 enabled；目录不可用/资源失效明确失败。

    只处理请求中实际提供的资源字段。目录 unavailable（feature-disabled 或
    传输/协议错误）时明确返回 503，绝不静默保存只有标签的能力。
    """
    checked: dict = {}
    for field in RESOURCE_FIELDS:
        if field not in updates:
            continue
        ids: list[str] = updates[field]
        if not ids:
            checked[field] = []
            continue
        kind = resources.FIELD_KIND[field]
        projected = await resources.project_kind(adapter, kind)
        if not projected["available"]:
            raise HTTPException(
                status_code=503,
                detail={
                    "errors": {
                        field: f"资源目录不可用：{projected['reason']}",
                    },
                },
            )
        by_id = {item["id"]: item for item in projected["items"]}
        missing = [identifier for identifier in ids if identifier not in by_id]
        disabled = [
            identifier
            for identifier in ids
            if identifier in by_id and not by_id[identifier]["enabled"]
        ]
        # 本版本不支持装配的资源（如 stdio/stateful MCP）：明确拒绝保存，绝不
        # 静默保存一个只存标签、实际无法装配的能力。
        unsupported = [
            identifier
            for identifier in ids
            if identifier in by_id
            and not by_id[identifier].get("selectable", True)
        ]
        if missing or disabled or unsupported:
            errors: dict[str, str] = {}
            if missing:
                errors[field] = "以下资源不存在：" + "、".join(missing)
            if disabled:
                errors[field] = "以下资源未启用：" + "、".join(disabled)
            if unsupported:
                errors[field] = "以下资源当前版本不支持：" + "、".join(unsupported)
            raise HTTPException(status_code=422, detail={"errors": errors})
        checked[field] = list(ids)
    return checked


def _native_name_prompt(agent: dict | None) -> tuple[str | None, str | None]:
    """从原生 AgentView 提取合法 name/system_prompt；非法一律视为缺失。"""
    if not isinstance(agent, dict):
        return None, None
    data = agent.get("data")
    if not isinstance(data, dict):
        return None, None
    name = data.get("name")
    prompt = data.get("system_prompt")
    return (
        name if isinstance(name, str) else None,
        prompt if isinstance(prompt, str) else None,
    )


def _public(row: Employee, *, native: dict | None = None) -> dict:
    """简洁稳定的业务响应：结合本地扩展与原生记录。

    姓名只有在原生记录合法时才作为原生事实展示；原生缺失/非法时退回本地
    索引值并显式 ``name_source="local"`` / ``native_available=False`` /
    非 ready，绝不用本地值冒充原生事实。
    """
    native_name, _ = _native_name_prompt(native)
    if native_name is not None:
        name = native_name
        name_source = "native"
        native_available = True
        sync_state = row.sync_state
    else:
        name = row.name
        name_source = "local"
        native_available = False
        sync_state = (
            SYNC_INCOMPLETE if row.sync_state == SYNC_READY else row.sync_state
        )
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "name": name,
        "name_source": name_source,
        "domain": row.domain,
        "responsibilities": row.responsibilities,
        "instructions": row.instructions,
        "avatar": row.avatar,
        "enabled": row.enabled,
        "knowledge": list(row.knowledge or []),
        "skills": list(row.skills or []),
        "tools": list(row.tools or []),
        "native_available": native_available,
        "sync_state": sync_state,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def _public_row_safe(
    adapter: AgentScopeHTTPClient,
    row: Employee,
) -> dict:
    """尽量带上原生事实；上游读取失败时退回本地非 ready 展示。

    仅用于 reconcile 读取（操作已明确落库、不是写成功响应）；写成功路径必须
    使用 :func:`_confirmed_public`，读不到原生确认时明确报错而非冒充保存成功。
    """
    if row.agent_id is None:
        return _public(row)
    try:
        agent = await adapter.find_agent(row.agent_id)
    except UpstreamError:
        return _public(row)
    return _public(row, native=agent)


async def _confirmed_public(
    adapter: AgentScopeHTTPClient,
    row: Employee,
) -> dict:
    """写/读成功后必须能读到有效原生确认；读不到就明确报未完成，不冒充成功。"""
    if row.agent_id is None:
        return _public(row)
    try:
        agent = await adapter.find_agent(row.agent_id)
    except UpstreamError as exc:
        raise _unknown_upstream_http(
            exc,
            message=(
                "操作已提交，但读取原生确认失败。员工保留员工 ID "
                f"{row.id}，请稍后重试获取。"
            ),
            employee_id=row.id,
            sync_state=row.sync_state,
        ) from exc
    native_name, native_prompt = _native_name_prompt(agent)
    if native_name is None or native_prompt is None:
        raise HTTPException(
            status_code=502,
            detail=_pending_detail(
                row.id,
                sync_state=row.sync_state,
                message="无法读取有效的原生记录确认结果，请稍后重试获取。",
            ),
        )
    return _public(row, native=agent)


def _upstream_http(exc: UpstreamError) -> HTTPException:
    """把上游失败收敛为明确 HTTP 错误，绝不伪装成功。"""
    if exc.status_code is None:
        return HTTPException(
            status_code=504,
            detail=(
                "无法确认 AgentScope 操作结果（超时或连接失败）："
                f"{exc.public_message}"
            ),
        )
    return HTTPException(
        status_code=502,
        detail=f"AgentScope 上游返回错误：{exc.public_message}",
    )


def _definitely_not_received(exc: UpstreamError) -> bool:
    """仅明确未受理的 4xx 可判定原生未创建/未修改。

    传输失败（``status_code is None``）、5xx 与异常 2xx 协议响应都无法证明
    原生未受理：请求可能已被处理，因此必须与超时同样保留占位与写入意图，
    由 ``reconcile`` 严格核对标记，绝不重发非幂等请求。
    """
    return exc.status_code is not None and 400 <= exc.status_code < 500


def _pending_detail(
    employee_id: str,
    *,
    sync_state: str,
    message: str,
) -> dict:
    return {
        "message": message,
        "employee_id": employee_id,
        "sync_state": sync_state,
    }


def _unknown_upstream_http(
    exc: UpstreamError,
    *,
    message: str,
    employee_id: str,
    sync_state: str,
) -> HTTPException:
    """结果未确认（超时/5xx/异常 2xx）：明确返回未完成与可核对员工 ID。"""
    return HTTPException(
        status_code=504 if exc.status_code is None else 502,
        detail=_pending_detail(
            employee_id,
            sync_state=sync_state,
            message=message,
        ),
    )


async def _find_conflict(
    db: Database,
    *,
    name: str,
    domain: str,
    exclude_id: str | None = None,
) -> dict[str, str] | None:
    """忽略大小写检查名称/领域唯一性，返回逐字段冲突说明。"""
    async with db.session() as session:
        stmt = select(Employee).where(
            or_(
                Employee.normalized_name == normalize(name),
                Employee.normalized_domain == normalize(domain),
            ),
        )
        if exclude_id is not None:
            stmt = stmt.where(Employee.id != exclude_id)
        rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return None
    conflicts: dict[str, str] = {}
    for row in rows:
        if row.normalized_name == normalize(name):
            conflicts["name"] = "员工名称已存在（忽略大小写）"
        if row.normalized_domain == normalize(domain):
            conflicts["domain"] = "责任领域已被其他员工使用（忽略大小写）"
    return conflicts


def _build_placeholder(
    *,
    employee_id: str,
    values: dict,
    intent: dict,
    write_revision: int,
) -> Employee:
    return Employee(
        id=employee_id,
        agent_id=None,
        name=values["name"],
        normalized_name=normalize(values["name"]),
        domain=values["domain"],
        normalized_domain=normalize(values["domain"]),
        responsibilities=values["responsibilities"],
        instructions=values["instructions"],
        avatar=values["avatar"],
        enabled=values["enabled"],
        knowledge=list(values.get("knowledge") or []),
        skills=list(values.get("skills") or []),
        tools=list(values.get("tools") or []),
        sync_state=SYNC_CREATING,
        write_revision=write_revision,
        write_intent=intent,
    )


def _build_intent(
    *,
    op: str,
    values: dict,
    prompt: str,
    revision: int,
    old_native: dict | None = None,
    generation_id: str | None = None,
) -> dict:
    intent = {"op": op, "prompt": prompt, "revision": revision}
    for key in _INTENT_FIELDS:
        intent[key] = values[key]
    if old_native is not None:
        # 真实原生旧值快照，仅用于明确未受理时的精确核对，不用本地重编译值
        # 冒充原生旧值。
        intent["old_native"] = old_native
    if generation_id is not None:
        # 创建意图：POST 之前持久化的实际服务代次，用于判断旧单进程请求是否
        # 已随重启退出。
        intent["generation_id"] = generation_id
    return intent


# ----------------------------------------------------------------------
# 数据库短事务助手（网络 I/O 绝不放在这些事务内）
# ----------------------------------------------------------------------
async def _reload_row(db: Database, employee_id: str) -> Employee | None:
    async with db.session() as session:
        return await session.get(Employee, employee_id)


async def _persist_intent(
    db: Database,
    employee_id: str,
    *,
    sync_state: str,
    intent: dict,
) -> None:
    """网络调用前持久化非 ready 与最少写入意图。失败即抛出，不继续调用。

    同一事务里把 ``write_revision`` 提升到本次意图修订：修订失败也不回退，
    确保迟到的旧请求无法通过低修订校验。
    """
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            raise RuntimeError("员工记录在持久化写入意图时不存在")
        row.sync_state = sync_state
        row.write_intent = intent
        revision = intent.get("revision")
        if isinstance(revision, int) and revision > row.write_revision:
            row.write_revision = revision
        await session.commit()


async def _persist_create_generation(
    db: Database,
    employee_id: str,
    generation_id: str,
) -> None:
    """创建 POST 之前把实际服务代次写入已持久化的创建意图。"""
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            raise RuntimeError("员工占位记录在持久化服务代次时不存在")
        intent = row.write_intent if isinstance(row.write_intent, dict) else {}
        intent = {**intent, "generation_id": generation_id}
        row.write_intent = intent
        await session.commit()


async def _apply_intent_ready(
    db: Database,
    employee_id: str,
    intent: dict,
) -> None:
    """写入意图确认生效：落本地字段并清除意图恢复 ready。"""
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            raise RuntimeError("员工记录在落地写入意图时不存在")
        row.name = intent["name"]
        row.normalized_name = normalize(intent["name"])
        row.domain = intent["domain"]
        row.normalized_domain = normalize(intent["domain"])
        row.responsibilities = intent["responsibilities"]
        row.instructions = intent["instructions"]
        row.avatar = intent["avatar"]
        # 默认资源关联与启停与原生写入在同一恢复点落地：原生未确认前绝不
        # 先应用本地资源/启停，避免“原生失败但资源已生效”的部分成功。
        if "enabled" in intent:
            row.enabled = bool(intent["enabled"])
        for field in RESOURCE_FIELDS:
            if field in intent:
                setattr(row, field, list(intent[field] or []))
        row.sync_state = SYNC_READY
        row.write_intent = None
        await session.commit()


async def _clear_intent_ready(db: Database, employee_id: str) -> None:
    """操作未生效：清除意图恢复 ready，允许后续重试。"""
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            return
        row.sync_state = SYNC_READY
        row.write_intent = None
        await session.commit()


def _is_disable_only(updates: dict) -> bool:
    """请求是否只是“禁用”这一可持久的安全操作。"""
    return (
        bool(updates)
        and set(updates) <= {"enabled"}
        and updates.get("enabled") is False
    )


async def _persist_enabled(
    db: Database,
    employee_id: str,
    enabled: bool,
) -> None:
    """只持久化启停本地字段（短事务），不触碰原生写入意图。"""
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            raise RuntimeError("员工记录在持久化启停状态时不存在")
        row.enabled = enabled
        await session.commit()


async def _bind_created(
    db: Database,
    employee_id: str,
    agent_id: str,
) -> None:
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            raise RuntimeError("员工占位记录在绑定原生 Agent 时不存在")
        row.agent_id = agent_id
        row.sync_state = SYNC_READY
        row.write_intent = None
        await session.commit()


async def _mark_incomplete(db: Database, employee_id: str) -> None:
    """结果不明确：标记 incomplete（保留意图）禁止新运行，供后续核对。"""
    async with db.session() as session:
        row = await session.get(Employee, employee_id)
        if row is None:
            return
        row.sync_state = SYNC_INCOMPLETE
        await session.commit()


async def _delete_placeholder(db: Database, employee_id: str) -> bool:
    """尽力删除本地创建占位（不触碰原生），返回是否确认清理成功。"""
    try:
        async with db.session() as session:
            row = await session.get(Employee, employee_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
        return True
    except Exception:  # noqa: BLE001 - 清理失败须如实报告
        return False


async def _find_agents_by_marker(
    adapter: AgentScopeHTTPClient,
    employee_id: str,
) -> list[dict]:
    """按稳定员工 ID 严格匹配原生 Agent 记录。

    只认编译 prompt **末尾规范**的“员工 ID 标记行 + 修订标记最后一行”对中的
    员工 ID；职责/指令正文即使引用了该员工 ID（携带相同标记文本）也不匹配，
    避免把别的员工误当成本次创建的唯一记录而错误绑定。
    """
    listing = await adapter.list_agents()
    agents = listing.get("agents")
    if not isinstance(agents, list):
        raise UpstreamError(
            "GET",
            "/agent/",
            200,
            "Agent 列表响应缺少 agents 数组",
        )
    matches: list[dict] = []
    for agent in agents:
        _, prompt = _native_name_prompt(agent)
        found_id, _ = parse_prompt_markers(prompt)
        if found_id == employee_id:
            matches.append(agent)
    return matches


async def _read_generation(adapter: AgentScopeHTTPClient) -> str | None:
    """读取 AgentScope 服务当前代次标识；不可读时返回 ``None``。

    代次是服务每次 build/start 新生成的非空 UUID；不可读（未就绪、连接失败、
    字段缺失）时调用方不得继续创建 POST，也不能据此猜测旧请求已退出。
    """
    try:
        health = await adapter.extension_health()
    except UpstreamError:
        return None
    generation = health.get("generation_id") if isinstance(health, dict) else None
    if isinstance(generation, str) and generation:
        return generation
    return None


async def _resolve_update_intent(
    db: Database,
    adapter: AgentScopeHTTPClient,
    employee_id: str,
    row: Employee,
    intent: dict | None,
    *,
    definite: bool,
) -> tuple[Employee | None, str]:
    """核对编辑写入意图，绝不凭一次旧值读取就恢复 ready。

    * 原生与目标一致 → 恢复 ready（``recovered``）。
    * ``definite``（明确未受理 4xx）且原生仍为精确旧快照 → 恢复 ready
      （``reverted``），允许重试。
    * 未知结果且原生仍为旧值 → 对相同目标幂等重试 PATCH，确认 desired 后才
      ready；重试仍未知则保持 pending（``upstream_error``），不误判未生效。
    * 其他情况无法判定 → 保持 incomplete（``ambiguous``）。
    """
    if row.agent_id is None:
        await _mark_incomplete(db, employee_id)
        return await _reload_row(db, employee_id), "ambiguous"
    try:
        agent = await adapter.find_agent(row.agent_id)
    except UpstreamError:
        return row, "upstream_error"
    if agent is None:
        await _mark_incomplete(db, employee_id)
        return await _reload_row(db, employee_id), "native_missing"
    native_name, native_prompt = _native_name_prompt(agent)
    if intent is None or native_name is None or native_prompt is None:
        await _mark_incomplete(db, employee_id)
        return await _reload_row(db, employee_id), "ambiguous"
    desired_name = intent.get("name")
    desired_prompt = intent.get("prompt")
    if not isinstance(desired_name, str) or not isinstance(
        desired_prompt, str
    ):
        await _mark_incomplete(db, employee_id)
        return await _reload_row(db, employee_id), "ambiguous"
    intended = intent.get("revision")
    intended = intended if isinstance(intended, int) else None
    if native_name == desired_name and native_prompt == desired_prompt:
        if definite:
            # 明确未受理，但原生已是目标值：直接恢复 ready。
            await _apply_intent_ready(db, employee_id, intent)
            return await _reload_row(db, employee_id), "recovered"
        # 未知结果：不得仅凭一次 GET 已 desired 就绕过旧 handler 屏障，
        # 必须走同修订幂等 PATCH 通过服务写屏障后再确认。
    old = intent.get("old_native")
    old_matches = (
        isinstance(old, dict)
        and native_name == old.get("name")
        and native_prompt == old.get("prompt")
    )
    if old_matches and definite:
        # 明确未受理且原生仍是精确旧快照：清除意图恢复 ready，允许重试。
        await _clear_intent_ready(db, employee_id)
        return await _reload_row(db, employee_id), "reverted"
    if not definite:
        # 未知结果：对相同目标、相同修订幂等重试 PATCH，经服务写屏障确认
        # exact desired 后才允许 ready；一次旧值读取不足以判定未生效。
        try:
            result = await adapter.update_agent(
                row.agent_id,
                name=desired_name,
                system_prompt=desired_prompt,
                write_revision=intended,
            )
        except UpstreamError as exc:
            if _definitely_not_received(exc):
                # 服务按修订/其他规则明确拒绝（如更高修订已存在）：不得 ready。
                await _mark_incomplete(db, employee_id)
                return await _reload_row(db, employee_id), "ambiguous"
            return row, "upstream_error"
        retry_name, retry_prompt = _native_name_prompt(result)
        if retry_name == desired_name and retry_prompt == desired_prompt:
            await _apply_intent_ready(db, employee_id, intent)
            return await _reload_row(db, employee_id), "recovered"
        await _mark_incomplete(db, employee_id)
        return await _reload_row(db, employee_id), "ambiguous"
    await _mark_incomplete(db, employee_id)
    return await _reload_row(db, employee_id), "ambiguous"


async def _reconcile_pending(
    db: Database,
    adapter: AgentScopeHTTPClient,
    employee_id: str,
) -> tuple[Employee | None, str]:
    """核对原生记录与写入意图，尝试把未完成写入恢复到明确状态。

    返回 ``(row, status)``，status 取值：
    ``ready`` / ``recovered`` / ``reverted`` / ``not_found`` /
    ``ambiguous`` / ``native_missing`` / ``upstream_error`` / ``missing``。
    """
    row = await _reload_row(db, employee_id)
    if row is None:
        return None, "missing"
    if row.sync_state == SYNC_READY:
        return row, "ready"
    intent = row.write_intent if isinstance(row.write_intent, dict) else None

    if row.agent_id is None:
        # 创建意图：只核对**末尾规范员工 ID**且唯一的真实原生记录，且绑定前
        # 必须与创建意图的 name/prompt 完全一致；不自动再 POST，也不猜。
        try:
            matches = await _find_agents_by_marker(adapter, employee_id)
        except UpstreamError:
            return row, "upstream_error"
        if len(matches) == 1:
            match = matches[0]
            # 绑定前核对原生 name/prompt 与创建意图完全一致：即使末尾员工 ID
            # 相同，配置已偏离意图也不能猜成 ready。
            desired_name = intent.get("name") if intent else None
            desired_prompt = intent.get("prompt") if intent else None
            native_name, native_prompt = _native_name_prompt(match)
            if (
                isinstance(desired_name, str)
                and isinstance(desired_prompt, str)
                and native_name == desired_name
                and native_prompt == desired_prompt
            ):
                await _bind_created(db, employee_id, match["id"])
                return await _reload_row(db, employee_id), "recovered"
            await _mark_incomplete(db, employee_id)
            return await _reload_row(db, employee_id), "ambiguous"
        if not matches:
            return row, "not_found"
        await _mark_incomplete(db, employee_id)
        return await _reload_row(db, employee_id), "ambiguous"

    # 编辑意图：读取原生当前值，与意图/真实旧快照比较判定实际结果。
    return await _resolve_update_intent(
        db,
        adapter,
        employee_id,
        row,
        intent,
        definite=False,
    )


async def _reconcile_or_http(
    db: Database,
    adapter: AgentScopeHTTPClient,
    employee_id: str,
) -> tuple[Employee | None, str]:
    """核对失败（本地状态更新写不进去）时明确报错，绝不谎称已标记成功。"""
    try:
        return await _reconcile_pending(db, adapter, employee_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        fallback = await _reload_row(db, employee_id)
        state = fallback.sync_state if fallback is not None else "unknown"
        raise HTTPException(
            status_code=500,
            detail=_pending_detail(
                employee_id,
                sync_state=state,
                message=(
                    "核对原生记录时无法更新本地状态，未确认状态未被更改，"
                    "请重试 reconcile。"
                ),
            ),
        ) from exc


def _employee_lock(request: Request, key: str) -> asyncio.Lock:
    """单进程内按员工串行化创建绑定/编辑/恢复的整个业务操作。

    该异步锁覆盖原生网络调用序列（``POST``/``PATCH``/核对读取）；只有数据库
    事务保持短小、不在事务内做网络 I/O。不引入多副本分布式锁框架。
    """
    locks: dict[str, asyncio.Lock] = request.app.state.employee_locks
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def register_employee_routes(app: FastAPI) -> None:
    @app.get("/api/employees", tags=["employees"])
    async def list_employees(request: Request, q: str | None = None) -> dict:
        """员工列表，``q`` 按原生姓名/责任领域/职责说明搜索。"""
        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        async with db.session() as session:
            rows = (
                (await session.execute(select(Employee))).scalars().all()
            )
        try:
            listing = await adapter.list_agents()
        except UpstreamError as exc:
            raise _upstream_http(exc) from exc
        agents = listing.get("agents")
        if not isinstance(agents, list):
            raise HTTPException(
                status_code=502,
                detail="AgentScope Agent 列表响应格式异常，无法核对员工关联。",
            )
        by_id = {
            agent.get("id"): agent
            for agent in agents
            if isinstance(agent, dict) and isinstance(agent.get("id"), str)
        }
        needle = q.strip().casefold() if q else None
        items = []
        for row in rows:
            native = by_id.get(row.agent_id) if row.agent_id else None
            native_name, _ = _native_name_prompt(native)
            # 正常数据按原生姓名搜索；原生姓名不可用时退回本地索引值，
            # 但响应中会显式标记非原生事实，不冒充已生效配置。
            search_name = native_name or row.name
            if needle and not any(
                needle in field.casefold()
                for field in (
                    search_name,
                    row.domain,
                    row.responsibilities,
                )
            ):
                continue
            items.append(_public(row, native=native))
        return {"employees": items, "total": len(items)}

    @app.post("/api/employees", status_code=201, tags=["employees"])
    async def create_employee(
        request: Request,
        payload: EmployeeCreate,
    ) -> dict:
        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        try:
            values = validate_create(payload)
        except EmployeeValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"errors": exc.errors},
            ) from exc
        # 先核对真实目录再落占位/调原生：资源失效或目录不可用明确失败。
        values.update(await _validate_selected_resources(adapter, values))

        employee_id = new_employee_id()
        # 同一员工从落占位到原生 POST→bind 的整个过程都在员工锁内，reconcile
        # 的核对/确认释放也在同一锁内，避免在途 POST 期间占位被并发确认删除而
        # 产生孤立原生 Agent。数据库事务仍保持短小，不在事务内做网络 I/O。
        async with _employee_lock(request, employee_id):
            conflict = await _find_conflict(
                db,
                name=values["name"],
                domain=values["domain"],
            )
            if conflict is not None:
                raise HTTPException(409, detail={"errors": conflict})

            system_prompt = compile_system_prompt(
                employee_id=employee_id,
                name=values["name"],
                domain=values["domain"],
                responsibilities=values["responsibilities"],
                instructions=values["instructions"],
                revision=CREATE_REVISION,
            )
            intent = _build_intent(
                op="create",
                values=values,
                prompt=system_prompt,
                revision=CREATE_REVISION,
            )
            row = _build_placeholder(
                employee_id=employee_id,
                values=values,
                intent=intent,
                write_revision=CREATE_REVISION,
            )
            # 先落非 ready 占位：名称/领域唯一约束兜底并发，落败者不调原生。
            try:
                async with db.session() as session:
                    session.add(row)
                    await session.commit()
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "errors": {
                            "name": "员工名称或责任领域已被占用（并发冲突）",
                        },
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail="本地无法保存创建占位，未调用原生，请重试。",
                ) from exc

            # POST 之前读取并持久化实际服务代次：健康预检不可读时不调用 POST，
            # 并准确清理尚未发送的占位（不产生孤立原生 Agent）。旧请求是否
            # 已退出只能由“代次已变化”证明，不能靠用户确认猜测。
            generation_id = await _read_generation(adapter)
            if generation_id is None:
                cleaned = await _delete_placeholder(db, employee_id)
                if not cleaned:
                    raise HTTPException(
                        status_code=500,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=SYNC_CREATING,
                            message=(
                                "无法读取 AgentScope 服务代次，且清理本地占位"
                                "失败，员工保留未完成状态，请调用 reconcile "
                                "核对释放。"
                            ),
                        ),
                    )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "无法读取 AgentScope 服务代次，未调用原生创建。"
                        "请确认服务已就绪后重试。"
                    ),
                )
            try:
                await _persist_create_generation(
                    db,
                    employee_id,
                    generation_id,
                )
            except Exception as exc:  # noqa: BLE001
                cleaned = await _delete_placeholder(db, employee_id)
                if not cleaned:
                    raise HTTPException(
                        status_code=500,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=SYNC_CREATING,
                            message=(
                                "本地无法记录服务代次，且清理占位失败，员工"
                                "保留未完成状态，请调用 reconcile 核对释放。"
                            ),
                        ),
                    ) from exc
                raise HTTPException(
                    status_code=500,
                    detail="本地无法记录服务代次，未调用原生创建，请重试。",
                ) from exc

            try:
                agent_id = await adapter.create_agent(
                    name=values["name"],
                    system_prompt=system_prompt,
                    generation_id=generation_id,
                )
            except UpstreamError as exc:
                if _definitely_not_received(exc):
                    # 明确未受理（4xx）：清理占位让用户重试；清理失败如实报告。
                    cleaned = await _delete_placeholder(db, employee_id)
                    if not cleaned:
                        raise HTTPException(
                            status_code=500,
                            detail=_pending_detail(
                                employee_id,
                                sync_state=SYNC_CREATING,
                                message=(
                                    "原生创建明确未受理，但清理本地占位失败，"
                                    "员工保留为未完成状态，请调用 reconcile "
                                    "核对释放。"
                                ),
                            ),
                        ) from exc
                    raise _upstream_http(exc) from exc
                # 超时/连接失败/5xx/异常 2xx：结果未知，原生可能已创建。保留
                # 占位、意图与唯一预约，不重发非幂等 POST，等待 reconcile 核对。
                raise _unknown_upstream_http(
                    exc,
                    message=(
                        "创建结果未确认：AgentScope 超时、连接失败或返回异常"
                        "响应，原生 Agent 可能已创建。已保留未完成占位，请用"
                        f"该员工 ID {employee_id} 调用 reconcile 核对，不自动"
                        "重发。"
                    ),
                    employee_id=employee_id,
                    sync_state=SYNC_CREATING,
                ) from exc

            # 创建受理后读取原生确认；读不到就保留未完成，不冒充保存成功。
            try:
                agent = await adapter.find_agent(agent_id)
            except UpstreamError as exc:
                raise _unknown_upstream_http(
                    exc,
                    message=(
                        "原生 Agent 已创建，但读取原生确认失败。已保留未完成"
                        f"占位（员工 ID {employee_id}），请调用 reconcile 核对"
                        "恢复，不自动重发。"
                    ),
                    employee_id=employee_id,
                    sync_state=SYNC_CREATING,
                ) from exc
            native_name, native_prompt = _native_name_prompt(agent)
            if native_name is None or native_prompt is None:
                raise HTTPException(
                    status_code=502,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=SYNC_CREATING,
                        message=(
                            "原生创建响应不完整，无法确认保存结果。已保留未完成"
                            "占位，请调用 reconcile 核对。"
                        ),
                    ),
                )

            try:
                await _bind_created(db, employee_id, agent_id)
            except Exception as exc:  # noqa: BLE001
                # 原生已创建：保留意图与标记，reconcile 可据标记绑定恢复。
                raise HTTPException(
                    status_code=500,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=SYNC_CREATING,
                        message=(
                            "原生 Agent 已创建但本地关联保存失败，员工保留为"
                            "未完成状态，请调用 reconcile 按标记恢复。"
                        ),
                    ),
                ) from exc

            final = await _reload_row(db, employee_id)
            if final is None:
                raise HTTPException(
                    status_code=500,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=SYNC_CREATING,
                        message="员工记录在绑定原生后丢失，请调用 reconcile 核对。",
                    ),
                )
            return _public(final, native=agent)

    @app.get("/api/employees/{employee_id}", tags=["employees"])
    async def get_employee(request: Request, employee_id: str) -> dict:
        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        row = await _reload_row(db, employee_id)
        if row is None:
            raise HTTPException(status_code=404, detail="员工不存在。")
        if row.agent_id is None:
            # 创建结果未确认的占位：明确返回非 ready + 可核对员工 ID。
            return _public(row)
        try:
            agent = await adapter.find_agent(row.agent_id)
        except UpstreamError as exc:
            raise _upstream_http(exc) from exc
        if agent is None:
            raise HTTPException(
                status_code=502,
                detail=(
                    "员工配置不完整：关联的原生 AgentScope Agent 不存在。"
                ),
            )
        return _public(row, native=agent)

    @app.patch("/api/employees/{employee_id}", tags=["employees"])
    async def update_employee(
        request: Request,
        employee_id: str,
        payload: EmployeeUpdate,
    ) -> dict:
        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        lock = _employee_lock(request, employee_id)
        async with lock:
            row = await _reload_row(db, employee_id)
            if row is None:
                raise HTTPException(status_code=404, detail="员工不存在。")

            # 先恢复此前的未完成写入，再在同一串行边界内执行本次编辑。
            try:
                updates = validate_update(payload)
            except EmployeeValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"errors": exc.errors},
                ) from exc
            # 核对本次请求实际提供的资源关联；目录不可用/资源失效明确失败。
            updates.update(
                await _validate_selected_resources(adapter, updates),
            )

            if row.sync_state != SYNC_READY:
                row, status = await _reconcile_or_http(db, adapter, employee_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="员工不存在。")
                if status not in ("ready", "recovered", "reverted"):
                    # 未完成写入期间只允许“禁用”这一可持久的安全操作；启用、
                    # 资源或职责编辑必须等 reconcile 到一致状态后再进行。
                    if _is_disable_only(updates):
                        await _persist_enabled(db, employee_id, False)
                        refreshed = await _reload_row(db, employee_id)
                        if refreshed is None:
                            raise HTTPException(
                                status_code=404,
                                detail="员工不存在。",
                            )
                        return await _public_row_safe(adapter, refreshed)
                    raise HTTPException(
                        status_code=409,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=row.sync_state,
                            message=(
                                "该员工存在未完成的写入，请先调用 reconcile "
                                "核对为一致状态后再编辑。"
                            ),
                        ),
                    )

            if not updates:
                return await _confirmed_public(adapter, row)

            # 启用只允许在一致（ready）且未删除状态下进行；禁用始终可持久。
            if updates.get("enabled") is True and row.sync_state != SYNC_READY:
                raise HTTPException(
                    status_code=409,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=row.sync_state,
                        message=(
                            "员工存在未完成的写入，只有在一致状态后才允许启用。"
                        ),
                    ),
                )

            # 编辑前读取真实原生 name/prompt 作为旧值快照，绝不使用本地重编译
            # 值冒充原生旧值；该快照仅用于明确未受理时的精确核对。
            try:
                agent = await adapter.find_agent(row.agent_id)
            except UpstreamError as exc:
                raise _upstream_http(exc) from exc
            if agent is None:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "员工配置不完整：关联的原生 AgentScope Agent 不存在。"
                    ),
                )
            native_name, native_prompt = _native_name_prompt(agent)
            if native_name is None or native_prompt is None:
                raise HTTPException(
                    status_code=502,
                    detail="原生 Agent 记录不完整，无法安全编辑。",
                )

            merged = {
                "name": updates.get("name", native_name),
                "domain": updates.get("domain", row.domain),
                "responsibilities": updates.get(
                    "responsibilities",
                    row.responsibilities,
                ),
                "instructions": updates.get(
                    "instructions",
                    row.instructions,
                ),
                "avatar": updates.get("avatar", row.avatar),
                "enabled": updates.get("enabled", row.enabled),
                "knowledge": updates.get(
                    "knowledge",
                    list(row.knowledge or []),
                ),
                "skills": updates.get("skills", list(row.skills or [])),
                "tools": updates.get("tools", list(row.tools or [])),
            }
            conflict = await _find_conflict(
                db,
                name=merged["name"],
                domain=merged["domain"],
                exclude_id=row.id,
            )
            if conflict is not None:
                raise HTTPException(409, detail={"errors": conflict})

            native_revision = parse_prompt_revision(native_prompt)
            comparison_prompt = compile_system_prompt(
                employee_id=row.id,
                name=merged["name"],
                domain=merged["domain"],
                responsibilities=merged["responsibilities"],
                instructions=merged["instructions"],
                revision=(
                    native_revision if native_revision is not None else 0
                ),
            )
            native_changed = (
                merged["name"] != native_name
                or comparison_prompt != native_prompt
            )
            if not native_changed:
                # 只改本地字段（如头像）：不触碰原生，直接落库恢复 ready。
                try:
                    await _apply_intent_ready(db, employee_id, merged)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(
                        status_code=500,
                        detail="本地保存失败，请重试。",
                    ) from exc
                refreshed = await _reload_row(db, employee_id)
                if refreshed is None:
                    raise HTTPException(
                        status_code=500,
                        detail="本地保存后员工记录丢失，请重试。",
                    )
                return _public(refreshed, native=agent)

            # 真正发起原生写入：单调递增写入修订并编译进目标 prompt；修订在
            # 写入前随意图持久化，失败或恢复都不回退。
            new_revision = row.write_revision + 1
            new_prompt = compile_system_prompt(
                employee_id=row.id,
                name=merged["name"],
                domain=merged["domain"],
                responsibilities=merged["responsibilities"],
                instructions=merged["instructions"],
                revision=new_revision,
            )
            intent = _build_intent(
                op="update",
                values=merged,
                prompt=new_prompt,
                revision=new_revision,
                old_native={"name": native_name, "prompt": native_prompt},
            )
            # 网络调用前持久化非 ready、意图与旧值快照；失败则不调原生。
            try:
                await _persist_intent(
                    db,
                    employee_id,
                    sync_state=SYNC_UPDATING,
                    intent=intent,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "本地无法记录写入意图，未调用原生，请重试。"
                    ),
                ) from exc

            try:
                result = await adapter.update_agent(
                    row.agent_id,
                    name=merged["name"],
                    system_prompt=new_prompt,
                    write_revision=new_revision,
                )
            except UpstreamError as exc:
                if _definitely_not_received(exc):
                    # 明确未受理（4xx）：核对精确旧快照，仍是旧值才恢复 ready。
                    row, status = await _resolve_update_intent(
                        db,
                        adapter,
                        employee_id,
                        row,
                        intent,
                        definite=True,
                    )
                    if status == "reverted":
                        raise _upstream_http(exc) from exc
                    if status == "recovered" and row is not None:
                        return await _confirmed_public(adapter, row)
                    raise HTTPException(
                        status_code=502,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=(
                                row.sync_state
                                if row is not None
                                else SYNC_UPDATING
                            ),
                            message=(
                                "上游明确未受理，但无法核对为一致状态，员工保留"
                                "未完成，请调用 reconcile 核对。"
                            ),
                        ),
                    ) from exc
                # 超时/连接失败/5xx/异常 2xx：结果未知，保留意图等待 reconcile
                # 幂等核对，绝不因一次旧值读取就恢复 ready。
                raise _unknown_upstream_http(
                    exc,
                    message=(
                        "编辑结果未确认：AgentScope 超时、连接失败或返回异常"
                        "响应，原生可能已提交。员工保留未完成状态，请调用 "
                        "reconcile 核对后再使用。"
                    ),
                    employee_id=employee_id,
                    sync_state=SYNC_UPDATING,
                ) from exc

            # 原生写响应可充分验证时直接复用；否则读取一次原生确认。
            result_name, result_prompt = _native_name_prompt(result)
            confirmed = (
                result_name == merged["name"] and result_prompt == new_prompt
            )
            if not confirmed:
                try:
                    confirmed_agent = await adapter.find_agent(row.agent_id)
                except UpstreamError as exc:
                    raise _unknown_upstream_http(
                        exc,
                        message=(
                            "原生编辑已提交，但读取原生确认失败。员工保留未完成"
                            "状态，请调用 reconcile 核对恢复。"
                        ),
                        employee_id=employee_id,
                        sync_state=SYNC_UPDATING,
                    ) from exc
                confirmed_name, confirmed_prompt = _native_name_prompt(
                    confirmed_agent
                )
                confirmed = (
                    confirmed_name == merged["name"]
                    and confirmed_prompt == new_prompt
                )
                result = confirmed_agent
            if not confirmed:
                await _mark_incomplete(db, employee_id)
                raise HTTPException(
                    status_code=502,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=SYNC_INCOMPLETE,
                        message=(
                            "无法确认原生编辑结果与目标一致，员工保持未完成，"
                            "请调用 reconcile 核对。"
                        ),
                    ),
                )

            # 原生已提交：落本地字段并清除意图恢复 ready。
            try:
                await _apply_intent_ready(db, employee_id, intent)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=SYNC_UPDATING,
                        message=(
                            "原生 Agent 已更新但本地保存失败，员工保留未完成"
                            "状态，请调用 reconcile 恢复。"
                        ),
                    ),
                ) from exc

            refreshed = await _reload_row(db, employee_id)
            if refreshed is None:
                raise HTTPException(
                    status_code=500,
                    detail="本地保存后员工记录丢失，请重试。",
                )
            return _public(refreshed, native=result)

    @app.post("/api/employees/{employee_id}/reconcile", tags=["employees"])
    async def reconcile_employee(
        request: Request,
        employee_id: str,
        payload: ReconcileRequest | None = None,
    ) -> dict:
        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        lock = _employee_lock(request, employee_id)
        # 核对与“确认未创建→释放占位”必须在同一员工锁内完成，避免与在途创建
        # 的原生 POST→bind 交错而误删占位。数据库事务仍保持短小。
        async with lock:
            row, status = await _reconcile_or_http(db, adapter, employee_id)

            if row is None:
                raise HTTPException(status_code=404, detail="员工不存在。")

            if status in ("ready", "recovered", "reverted"):
                return await _public_row_safe(adapter, row)

            if status == "not_found":
                confirm = bool(payload and payload.confirm_not_created)
                if not confirm:
                    raise HTTPException(
                        status_code=409,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=row.sync_state,
                            message=(
                                "未在原生找到该创建标记。如确认原生从未创建该"
                                "员工，可用 confirm_not_created 释放占位后重试"
                                "创建；否则请稍后再次核对。"
                            ),
                        ),
                    )
                # 释放创建占位需要证明“旧单进程 POST 已随重启退出”。只有当前
                # 服务代次与创建意图记录的服务代次明确不同，且原生标记仍不
                # 存在，才能安全释放；同一代次下不能靠用户确认猜测旧请求
                # 已结束。
                intent = (
                    row.write_intent
                    if isinstance(row.write_intent, dict)
                    else {}
                )
                recorded = intent.get("generation_id")
                gen_before = await _read_generation(adapter)
                if gen_before is None:
                    raise HTTPException(
                        status_code=503,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=row.sync_state,
                            message=(
                                "无法读取 AgentScope 服务代次，无法确认旧创建"
                                "请求是否已退出，暂不释放占位，请稍后重试。"
                            ),
                        ),
                    )
                if (
                    not isinstance(recorded, str)
                    or not recorded
                    or gen_before == recorded
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=row.sync_state,
                            message=(
                                "服务代次未变化，旧创建请求可能仍在进行，不能"
                                "释放占位。请先重启 AgentScope 服务使代次变化"
                                "后再核对释放。"
                            ),
                        ),
                    )
                # 代次已确认变化：**重新扫描**精确标记（绝不复用更早的 absence），
                # 并核对扫描前后代次稳定。竞态窗口内旧请求可能已在新代次落库为
                # 既有记录：重新扫到必须按原意图恢复，不能释放占位。
                try:
                    rescanned = await _find_agents_by_marker(adapter, employee_id)
                except UpstreamError as exc:
                    raise HTTPException(
                        status_code=504,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=row.sync_state,
                            message=(
                                "无法读取原生 Agent 目录核对创建标记，保留未完成"
                                "状态，请稍后重试。"
                            ),
                        ),
                    ) from exc
                gen_after = await _read_generation(adapter)
                if gen_after is None or gen_after != gen_before:
                    raise HTTPException(
                        status_code=409,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=row.sync_state,
                            message=(
                                "核对期间服务代次再次变化，无法确认创建结果，"
                                "保留未完成状态，请稍后重试。"
                            ),
                        ),
                    )
                if rescanned:
                    # 重新扫到既有记录：按原意图恢复（重新 reconcile 绑定），
                    # 绝不释放占位、绝不重复 POST。
                    recovered_row, recovered_status = await _reconcile_or_http(
                        db,
                        adapter,
                        employee_id,
                    )
                    if (
                        recovered_row is not None
                        and recovered_status in ("ready", "recovered", "reverted")
                    ):
                        return await _public_row_safe(adapter, recovered_row)
                    raise HTTPException(
                        status_code=409,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=(
                                recovered_row.sync_state
                                if recovered_row is not None
                                else row.sync_state
                            ),
                            message=(
                                "重新核对到原生创建记录但未能恢复一致，保留"
                                "未完成状态，请稍后重试 reconcile。"
                            ),
                        ),
                    )
                released = await _delete_placeholder(db, employee_id)
                if not released:
                    raise HTTPException(
                        status_code=500,
                        detail=_pending_detail(
                            employee_id,
                            sync_state=SYNC_CREATING,
                            message="释放创建占位失败，请重试。",
                        ),
                    )
                return {
                    "status": "released",
                    "employee_id": employee_id,
                    "sync_state": "released",
                }

            if status == "upstream_error":
                raise HTTPException(
                    status_code=504,
                    detail=_pending_detail(
                        employee_id,
                        sync_state=row.sync_state,
                        message="无法连接 AgentScope 核对原生记录，请稍后重试。",
                    ),
                )

            # ambiguous / native_missing：无法判定为一致，保持 incomplete。
            raise HTTPException(
                status_code=409,
                detail=_pending_detail(
                    employee_id,
                    sync_state=row.sync_state,
                    message=(
                        "无法核对为一致状态，员工保持未完成，禁止用于新运行。"
                    ),
                ),
            )
