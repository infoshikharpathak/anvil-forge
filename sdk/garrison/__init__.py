"""garrison: governed, traced agents on Anvil + Agent Forge.

    from garrison import Platform

    platform = Platform.from_config("garrison.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", "What are my accounts?")
"""

from .config import ConfigError, load_config
from .platform import NOT_AN_ACTION, FlowResult, NotRequested, Platform, PlatformError, RunResult
from .trace import DashboardSink, LocalSink, TraceExporter, TraceSink

__all__ = [
    "NOT_AN_ACTION",
    "NotRequested",
    "ConfigError", "DashboardSink", "FlowResult", "LocalSink", "Platform",
    "PlatformError", "RunResult", "TraceExporter", "TraceSink", "load_config",
]
