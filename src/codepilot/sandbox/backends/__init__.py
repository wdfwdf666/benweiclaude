"""Operating-system isolation backends."""

from .base import SandboxCapabilities
from .linux import BubblewrapBackend

__all__ = ["BubblewrapBackend", "SandboxCapabilities"]
