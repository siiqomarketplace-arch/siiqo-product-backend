"""Scripted fake model for the seller copilot tests (shared with the offline
scenario evals in app/services/ai/evals/copilot.py)."""
from app.services.ai.evals.copilot import ScriptedModel, final_answer


def answer(text, actions=None, answered=True):
    return final_answer(text, actions, answered)


__all__ = ["ScriptedModel", "answer"]
