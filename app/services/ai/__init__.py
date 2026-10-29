"""
Siiqo AI gateway (Phase 0B). Backend-internal only: no user-facing endpoints.

Use ``run(action, seller_id, inputs)`` and treat every ``AIError`` as
"use the non-AI fallback". See gateway.py for the full contract.
"""
from .errors import (
    AICapExceeded,
    AIDisabled,
    AIError,
    AIInvalidOutput,
    AIRefused,
    AIRequestError,
    AIUnavailable,
)
from .gateway import AIResult, GatewayConfig, kill_switch_state, run, set_runtime_kill_switch

__all__ = [
    "run", "AIResult", "GatewayConfig", "kill_switch_state", "set_runtime_kill_switch",
    "AIError", "AIDisabled", "AICapExceeded", "AIRefused", "AIUnavailable",
    "AIInvalidOutput", "AIRequestError",
]
