"""JARVIS: a persistent, inspectable intelligence substrate around an LLM.

This package implements the foundation layer of the JARVIS protocol:
persistent state, observability, an explicit task/verification model, and
governed memory with ranked retrieval.

It deliberately does NOT implement planning, agent orchestration, skills or
automation. Those are recorded in docs/BACKLOG.md and should only be built
when observed usage produces evidence that they are needed.
"""

__version__ = "0.1.0"

from jarvis.store import Store, open_store  # noqa: F401
