"""Typed errors raised by the AI gateway. Callers fall back to non-AI behaviour on any AIError."""


class AIError(Exception):
    """Base class for every gateway error."""

    def __init__(self, message: str = "", *, reason: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.reason = reason


class AIDisabled(AIError):
    """The kill switch is off (globally or for this action)."""


class AICapExceeded(AIError):
    """A per-seller action cap or the global daily cost cap has been reached."""


class AIRefused(AIError):
    """The model declined the request (stop_reason == "refusal")."""

    def __init__(self, message: str = "", *, category: str | None = None):
        super().__init__(message, reason="refusal")
        self.category = category


class AIUnavailable(AIError):
    """The provider cannot be reached or used right now (no key, SDK missing, outage, rate limit)."""


class AIInvalidOutput(AIError):
    """The model answered but the output was truncated or did not match the requested schema."""


class AIRequestError(AIError):
    """The request itself is wrong (unknown action, oversized input, 400 from the API). A code bug, not an outage."""
