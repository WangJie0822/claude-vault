"""Shared runtime primitives for the Context Vault plugin."""

from .runtime import HookContext, RuntimeKind, detect_runtime

__all__ = ["HookContext", "RuntimeKind", "detect_runtime"]
