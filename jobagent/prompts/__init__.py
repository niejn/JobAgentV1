"""System prompt content and layered assembly for the JobAgent."""

from __future__ import annotations

from jobagent.prompts.builder import ALL_GATING_TOOLS, build_system_prompt
from jobagent.prompts.main_agent import MAIN_AGENT_SYSTEM_PROMPT

__all__ = ["ALL_GATING_TOOLS", "MAIN_AGENT_SYSTEM_PROMPT", "build_system_prompt"]
