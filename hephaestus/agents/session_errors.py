"""Provider-neutral failures for resumable agent conversations."""


class AgentSessionLostError(RuntimeError):
    """An established provider session can no longer be resumed."""
