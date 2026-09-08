"""Small public error taxonomy for ReplayLab AgentHost."""


class ReplayLabAIError(RuntimeError):
    """Base error suitable for display in the embedded assistant panel."""


class ConfigurationError(ReplayLabAIError):
    """A provider profile or local executable is invalid."""


class ProviderCapabilityError(ConfigurationError):
    """A provider lacks one required, non-retryable protocol capability."""

    retryable = False

    def __init__(self, message: str, *, error_code: str, capability: str) -> None:
        if not error_code or not capability:
            raise ValueError("provider capability diagnostics require stable identifiers")
        super().__init__(message)
        self.error_code = error_code
        self.capability = capability


class ConfigurationConflictError(ConfigurationError):
    """Optimistic configuration state changed before an atomic mutation."""


class UnsafeConfigurationMutationError(ConfigurationError):
    """A requested configuration write crossed the metadata boundary."""


class AuthenticationError(ReplayLabAIError):
    """A required credential is unavailable or rejected."""


class ConnectorError(ReplayLabAIError):
    """A model or external-agent connector failed."""


class ContextOverflowError(ConnectorError):
    """Immutable request data cannot fit in the selected context window."""


class LifecycleCleanupError(ConnectorError):
    """An owned child transport may still be alive after cleanup."""


class BridgeError(ReplayLabAIError):
    """The qrenderdoc IPC bridge rejected or timed out a request."""


class PolicyError(ReplayLabAIError):
    """A tool or permission request exceeded its deterministic scope."""


__all__ = [
    "AuthenticationError",
    "BridgeError",
    "ConfigurationConflictError",
    "ConfigurationError",
    "ConnectorError",
    "ContextOverflowError",
    "LifecycleCleanupError",
    "PolicyError",
    "ProviderCapabilityError",
    "ReplayLabAIError",
    "UnsafeConfigurationMutationError",
]
