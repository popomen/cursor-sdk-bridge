"""Opus 5.5's SDK variants, shared by the adapters and client configuration."""
from dataclasses import dataclass


# Keep the original three IDs first, including the existing default selection.
EFFORTS = ("high", "xhigh", "max", "low", "medium")


@dataclass(frozen=True)
class Model:
    effort: str
    context: str
    fast: bool

    @property
    def display_name(self):
        return f"Opus 5.5 {self.effort} {self.context}" + (" fast" if self.fast else "")

    def selection(self):
        return {"id": "claude-opus-5-5", "params": [
            {"id": "context", "value": self.context},
            {"id": "effort", "value": self.effort},
            {"id": "fast", "value": str(self.fast).lower()}]}


MODELS = {
    f"claude-opus-5-5-{effort}" + ("-300k" if context == "300k" else "") + ("-fast" if fast else ""):
        Model(effort, context, fast)
    for context in ("1m", "300k") for fast in (False, True) for effort in EFFORTS
}


def claude_model(model):
    """Claude Code strips [1m] before sending the adapter's model ID."""
    return model + ("[1m]" if MODELS[model].context == "1m" else "")
