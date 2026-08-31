"""Authenticated third-party APIs (Gmail, Notion, GitHub, your own backend...).

Services are declared in ``services.json``: base URL plus which *environment
variable* holds the credential. The token is injected here, at call time - the
model never sees it and cannot set arbitrary headers, so it cannot leak a
credential to a host you did not configure.

See ``services.example.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from autonomous.config import Settings
from autonomous.tools.base import Tool

SAFE_METHODS = {"GET", "HEAD"}


@dataclass(slots=True)
class Service:
    name: str
    base_url: str
    description: str = ""
    auth_type: str = "none"  # none | bearer | header | query
    auth_env: str | None = None
    auth_name: str | None = None  # header or query-parameter name
    allow_writes: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Service:
        auth = data.get("auth") or {}
        return cls(
            name=data["name"],
            base_url=data["base_url"],
            description=data.get("description", ""),
            auth_type=auth.get("type", "none"),
            auth_env=auth.get("env"),
            auth_name=auth.get("name"),
            allow_writes=bool(data.get("allow_writes", False)),
        )

    @property
    def token(self) -> str | None:
        return os.environ.get(self.auth_env) if self.auth_env else None

    @property
    def configured(self) -> bool:
        return self.auth_type == "none" or bool(self.token)


def load_services(path: Path) -> list[Service]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [Service.from_dict(item) for item in data.get("services", [])]


async def call_service(
    services: dict[str, Service],
    settings: Settings,
    service: str,
    path: str,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    body: Any = None,
) -> str:
    svc = services.get(service)
    if svc is None:
        return f"Error: unknown service {service!r}. Configured: {', '.join(services) or 'none'}"
    if not svc.configured:
        return f"Error: service {service!r} needs {svc.auth_env} set in the environment."

    method = method.upper()
    if method not in SAFE_METHODS and not svc.allow_writes:
        return (
            f"Error: {method} is not allowed for {service!r}. "
            'Set "allow_writes": true for it in services.json to permit writes.'
        )

    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    params = dict(query or {})
    if svc.auth_type == "bearer":
        headers["Authorization"] = f"Bearer {svc.token}"
    elif svc.auth_type == "header" and svc.auth_name:
        headers[svc.auth_name] = svc.token or ""
    elif svc.auth_type == "query" and svc.auth_name:
        params[svc.auth_name] = svc.token or ""

    url = urljoin(svc.base_url.rstrip("/") + "/", path.lstrip("/"))
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        response = await client.request(
            method, url, headers=headers, params=params or None, json=body
        )
    text = response.text[:20_000]
    return f"HTTP {response.status_code} {method} {url}\n\n{text}"


def build_service_tools(settings: Settings, services: list[Service]) -> list[Tool]:
    if not services:
        return []
    by_name = {svc.name: svc for svc in services}
    catalogue = "\n".join(
        f"- {svc.name}: {svc.description or svc.base_url}"
        f"{'' if svc.configured else ' (credential missing)'}"
        f"{' [read-only]' if not svc.allow_writes else ''}"
        for svc in services
    )
    return [
        Tool(
            name="call_service",
            description=(
                "Call a configured third-party API. Authentication is added automatically.\n"
                f"Available services:\n{catalogue}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": sorted(by_name)},
                    "path": {"type": "string", "description": "Path relative to the base URL."},
                    "method": {
                        "type": "string",
                        "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                    },
                    "query": {"type": "object", "description": "Query-string parameters."},
                    "body": {"type": "object", "description": "JSON body for writes."},
                },
                "required": ["service", "path"],
            },
            fn=lambda service, path, method="GET", query=None, body=None: call_service(
                by_name, settings, service, path, method, query, body
            ),
            mutating=True,
        )
    ]
