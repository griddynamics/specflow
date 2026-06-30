"""Langfuse v4 tracing for SpecFlow agent workflows.

One trace per workflow step (kb_init, planning, per-phase, etc.). All traces for
a generation share session_id=generation_id so the Langfuse Sessions view groups
them. No-op when no Langfuse credentials are configured.
"""

from .client import LangfuseTracer, tracer
from .stream_tracer import LangfuseStreamTracer, _NullTracer

__all__ = ["LangfuseStreamTracer", "LangfuseTracer", "_NullTracer", "tracer"]
