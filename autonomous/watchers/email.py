"""IMAP inbox watcher.

Read-only: it fetches headers of recent messages and never marks anything as
seen, deletes, or replies. Set IMAP_HOST to enable it.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
from email.header import decode_header, make_header

from autonomous.config import Settings
from autonomous.watchers.base import Observation, Watcher


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


class EmailWatcher(Watcher):
    name = "email"

    def __init__(self, settings: Settings, fetch_limit: int = 20) -> None:
        self.settings = settings
        self.fetch_limit = fetch_limit

    @property
    def interval_seconds(self) -> int:
        return self.settings.email_poll_seconds

    @property
    def enabled(self) -> bool:
        s = self.settings
        return bool(s.imap_host and s.imap_user and s.imap_password)

    async def poll(self) -> list[Observation]:
        # imaplib is blocking, so the whole poll runs in a worker thread.
        return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[Observation]:
        s = self.settings
        conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
        try:
            conn.login(s.imap_user, s.imap_password)
            # readonly=True keeps \Seen flags untouched.
            conn.select(s.imap_mailbox, readonly=True)
            status, data = conn.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()[-self.fetch_limit :]
            observations: list[Observation] = []
            for msg_id in reversed(ids):
                status, msg_data = conn.fetch(
                    msg_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
                )
                if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                headers = email.message_from_bytes(msg_data[0][1])
                message_id = headers.get("Message-ID") or f"{s.imap_mailbox}:{msg_id.decode()}"
                subject = _decode(headers.get("Subject")) or "(no subject)"
                sender = _decode(headers.get("From"))
                observations.append(
                    Observation(
                        key=message_id.strip(),
                        title=f"{subject} - {sender}" if sender else subject,
                        body=None,
                        data={
                            "from": sender,
                            "subject": subject,
                            "date": headers.get("Date", ""),
                            "mailbox": s.imap_mailbox,
                        },
                    )
                )
            return observations
        finally:
            with contextlib.suppress(Exception):
                conn.logout()
