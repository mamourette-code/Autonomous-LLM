"""Command line entry points: serve the UI, run a single goal, poll a watcher."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from autonomous.agent import Agent
from autonomous.config import get_settings
from autonomous.providers import ProviderError, build_provider
from autonomous.storage import Database
from autonomous.tools import build_registry


def lan_address() -> str:
    """This machine's address on the local network.

    Opening a UDP socket to an outside address makes the OS pick the interface
    it would actually route through; nothing is sent.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return probe.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autonomous", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the local web UI (and the watchers)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument(
        "--lan",
        action="store_true",
        help="serve to your whole network so a phone or tablet can reach it; "
        "requires AUTH_TOKEN and prints the address to open",
    )

    run = sub.add_parser("run", help="run one goal from the terminal")
    run.add_argument("goal", nargs="+")
    run.add_argument("--provider")

    poll = sub.add_parser("poll", help="poll one watcher once and print what is new")
    poll.add_argument("watcher")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()

    if args.command == "serve":
        import uvicorn

        host = args.host or settings.host
        port = args.port or settings.port

        if args.lan:
            if not settings.auth_token:
                print(
                    "error: --lan needs an AUTH_TOKEN, or anyone on your network could "
                    "read your watchers and spend your API credits.\n"
                    '  Generate one:  python -c "import secrets; '
                    'print(secrets.token_urlsafe(24))"\n'
                    "  Then add it to .env as AUTH_TOKEN=...",
                    file=sys.stderr,
                )
                return 2
            host = "0.0.0.0"  # noqa: S104 - deliberate, and gated on a token
            address = lan_address()
            print("\n  Open this on your phone, tablet or another computer:\n")
            print(f"      http://{address}:{port}\n")
            print("  Sign in with the token from your .env file.")
            print("  Both devices must be on the same network.\n")

        uvicorn.run(
            "autonomous.web.app:app",
            factory=True,
            host=host,
            port=port,
            reload=args.reload,
        )
        return 0

    if args.command == "run":
        goal = " ".join(args.goal)
        db = Database(settings.db_path)
        try:
            provider = build_provider(settings, args.provider)
        except ProviderError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        agent = Agent(provider, build_registry(settings, db), db, settings)
        result = asyncio.run(agent.run(goal))
        print(result.answer or result.error or "")
        db.close()
        return 0 if result.status == "succeeded" else 1

    if args.command == "poll":
        from autonomous.watchers import build_scheduler

        db = Database(settings.db_path)
        scheduler = build_scheduler(settings, db)
        watcher = next((w for w in scheduler.watchers if w.name == args.watcher), None)
        if watcher is None:
            names = ", ".join(w.name for w in scheduler.watchers)
            print(f"error: no watcher {args.watcher!r}. Available: {names}", file=sys.stderr)
            return 2
        if not watcher.enabled:
            print(f"error: watcher {args.watcher!r} is not configured", file=sys.stderr)
            return 2
        new = asyncio.run(scheduler.poll_once(watcher))
        print(f"{args.watcher}: {new} new observation(s)")
        db.close()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
