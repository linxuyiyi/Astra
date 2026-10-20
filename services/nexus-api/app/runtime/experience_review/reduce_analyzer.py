"""跨片段归纳：在 Token 预算内合并 observation，输出双轨候选。"""

from __future__ import annotations

import json
from typing import Any

from app.config.settings import get_settings

from .harness_analyzer import annotate_harness_candidates
from .llm_client import ReviewLLMClient, ReviewLLMError
from .schemas import Candidate, CandidateList, Observation

SYSTEM = """你是独立的经验归纳器。输入是一组已结构化的局部观察，不是原始轨迹。
输出最多若干条候选经验，分为：
- domain：可跨任务复用的领域判断、处理方法与验证方式；
- harness_improvement：工具使用、上下文准备、技能调用、验证习惯等工作方法改进。
规则：
- 每条必须有 evidence_refs，只能引用输入 observation 中出现过的 trace id。
- 没有可复用价值时返回 {"candidates":[]}，不要凑数。
- 不得声称验证通过，除非 observation 明确记录了通过的验证；验证状态由平台校验。
- harness_improvement 必须给出 target_component 和 proposed_change，只能建议，不得执行。
只输出 JSON：
{"candidates":[
  {"kind":"domain","title":"","summary":"","content_md":"","experience_type":"domain",
   "scope":{},"evidence_refs":[],"tags":[],"target_component":null,"proposed_change":null}
]}"""


def _observation_view(item: Observation) -> dict[str, Any]:
    return item.model_dump()


def _batches(observations: list[Observation], budget_tokens: int) -> list[list[Observation]]:
    batches: list[list[Observation]] = []
    current: list[Observation] = []
    size = 0
    for observation in observations:
        tokens = max(1, len(json.dumps(_observation_view(observation), ensure_ascii=False)) // 4)
        if current and size + tokens > budget_tokens:
            batches.append(current)
            current = []
            size = 0
        current.append(observation)
        size += tokens
    if current:
        batches.append(current)
    return batches


async def _reduce_batch(
    llm: ReviewLLMClient, *, task_meta: dict[str, Any], observations: list[Observation]
) -> list[Candidate]:
    user = json.dumps(
        {"task": task_meta, "observations": [_observation_view(item) for item in observations]},
        ensure_ascii=False,
    )
    payload = await llm.complete_json(SYSTEM, user)
    try:
        return CandidateList.model_validate(payload).candidates
    except Exception as exc:  # noqa: BLE001 - model JSON boundary
        raise ReviewLLMError("bad_reduce_json", f"归纳 JSON 不合法: {exc}") from exc


async def reduce_observations(
    llm: ReviewLLMClient,
    *,
    task_meta: dict[str, Any],
    observations: list[Observation],
) -> list[Candidate]:
    if not observations:
        return []
    settings = get_settings()
    batches = _batches(observations, settings.experience_review_reduce_input_tokens)
    candidates: list[Candidate] = []
    for batch in batches:
        candidates.extend(await _reduce_batch(llm, task_meta=task_meta, observations=batch))
    # 去重并限制数量；证据校验在 EvidenceValidator 中完成。
    deduped: list[Candidate] = []
    seen: set[str] = set()
    for candidate in annotate_harness_candidates(candidates):
        key = candidate.title.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= settings.experience_review_max_candidates:
            break
    return deduped
