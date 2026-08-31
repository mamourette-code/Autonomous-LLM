"""Turning exceptions into messages a human (or a model) can act on.

Several network exceptions - httpx timeouts in particular - carry an empty
``str()``, which would otherwise surface in the UI as "ConnectTimeout: " with
nothing after it.
"""

from __future__ import annotations


def describe(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name
