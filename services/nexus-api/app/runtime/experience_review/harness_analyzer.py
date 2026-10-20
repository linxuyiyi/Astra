"""Harness 改进候选：只从结构化 observation 派生，不直接修改配置。"""

from __future__ import annotations

from .schemas import Candidate, Observation

ALLOWED_TARGETS = {
    "system_prompt",
    "tool_guidance",
    "skill",
    "memory",
    "guardrails",
    "scripts",
    "subagents",
    "plugins",
}


def annotate_harness_candidates(candidates: list[Candidate]) -> list[Candidate]:
    for candidate in candidates:
        if candidate.kind != "harness_improvement":
            continue
        target = (candidate.target_component or "").strip()
        if target not in ALLOWED_TARGETS:
            candidate.target_component = "tool_guidance"
        candidate.experience_type = "harness_improvement"
        candidate.scope = {
            **candidate.scope,
            "evaluation_level": "observed",
            "requires_further_validation": True,
        }
    return candidates


def harness_observations(observations: list[Observation]) -> list[Observation]:
    return [
        item
        for item in observations
        if item.correction.strip() and item.outcome.strip()
    ]
