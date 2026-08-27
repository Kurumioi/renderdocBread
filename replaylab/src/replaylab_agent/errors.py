class ReplayLabAIError(RuntimeError):
    """Base error suitable for display in the embedded assistant panel."""


class ConfigurationError(ReplayLabAIError):
    """A provider profile or local executable is invalid."""


class AuthenticationError(ReplayLabAIError):
    """A required credential is unavailable or rejected."""


class ConnectorError(ReplayLabAIError):
    """A model or external-agent connector failed."""


class EvidenceValidationError(ConnectorError):
    """A specialist answer is incomplete but may be corrected in the same task."""

    def __init__(
        self,
        message: str,
        *,
        partial_text: str = "",
        completed_tools: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.partial_text = partial_text
        self.completed_tools = completed_tools


class ContextOverflowError(ConnectorError):
    """Immutable request data cannot fit inside the selected model context window."""


class LifecycleCleanupError(ConnectorError):
    """An owned child transport may still be alive after cleanup was attempted."""


class BridgeError(ReplayLabAIError):
    """The qrenderdoc IPC bridge rejected or timed out a request."""


class PolicyError(ReplayLabAIError):
    """A tool, skill, or permission request exceeded its granted scope."""
