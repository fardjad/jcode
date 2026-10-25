"""Synchronous client for the versioned jcode harness API."""

from .client import (
    ConversationTurn,
    HarnessAPIError,
    HarnessClient,
    HarnessConnectionError,
    HarnessProtocolError,
    HarnessRequestError,
    HarnessResult,
    HarnessSession,
    HarnessTimeoutError,
    NativeSessionInfo,
    NativeToolDefinition,
    SessionBusyError,
    SessionSettlingError,
    UnsupportedCapabilityError,
)

__all__ = [
    "HarnessAPIError",
    "ConversationTurn",
    "HarnessClient",
    "HarnessConnectionError",
    "HarnessProtocolError",
    "HarnessRequestError",
    "HarnessResult",
    "HarnessSession",
    "HarnessTimeoutError",
    "NativeSessionInfo",
    "NativeToolDefinition",
    "SessionBusyError",
    "SessionSettlingError",
    "UnsupportedCapabilityError",
]

__version__ = "0.1.0"
