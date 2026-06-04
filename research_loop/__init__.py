"""AutoResearch primitives for MemeBot3.

The package is intentionally replay/paper-only.  Runtime trading code and live
surfaces are outside the mutable AutoResearch surface.
"""

__all__ = [
    "api_budget",
    "bandit",
    "batch_runner",
    "candidate_diff",
    "candidate_generator",
    "checkpoint",
    "evaluator",
    "experiment_schema",
    "llm_adapter",
    "objectives",
    "paper_forward",
    "policy_promoter",
    "replay_runner",
    "report_bundle",
    "rollback",
    "safety",
    "sandbox",
    "scheduler",
    "scoreboard",
    "search_space",
]
