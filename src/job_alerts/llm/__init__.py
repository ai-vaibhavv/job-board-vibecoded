"""LLM-based job assessment, with keyword scoring as the safety net."""

from .base import JobAssessment, LlmError, LlmProvider
from .chain import LlmAssessor, build_providers
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .providers import ColabProvider, parse_assessments

__all__ = [
    "SYSTEM_PROMPT",
    "ColabProvider",
    "JobAssessment",
    "LlmAssessor",
    "LlmError",
    "LlmProvider",
    "build_providers",
    "build_user_prompt",
    "parse_assessments",
]
