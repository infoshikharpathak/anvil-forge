"""bastion: governed, traced agents on Anvil + Agent Forge.

    from bastion import Platform

    platform = Platform.from_config("bastion.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", "What are my accounts?")
"""

from .config import ConfigError, load_config
from .platform import FlowResult, Platform, PlatformError, RunResult
from .trace import DashboardSink, LocalSink, TraceExporter, TraceSink

__all__ = [
    "ConfigError", "DashboardSink", "FlowResult", "LocalSink", "Platform",
    "PlatformError", "RunResult", "TraceExporter", "TraceSink", "load_config",
]
