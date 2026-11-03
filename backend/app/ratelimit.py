"""Sliding-window rate limits stored in the database (shared by every serverless instance)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from . import models as m
from .db import tx


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__("too many attempts")
        self.retry_after = retry_after


def hit(key: str, limit: int, window_s: int) -> None:
    """Record one attempt for key; raise RateLimited if more than `limit` happened within the window."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=window_s)
    t = m.rate_events
    with tx() as c:
        n = c.execute(select(func.count()).select_from(t).where(t.c.key == key, t.c.created_at > since)).scalar() or 0
        if n >= limit:
            raise RateLimited(window_s)
        c.execute(t.insert().values(key=key, created_at=now))
        if random.random() < 0.02:  # housekeeping
            c.execute(delete(t).where(t.c.created_at < now - timedelta(days=2)))


def clear(key: str) -> None:
    with tx() as c:
        c.execute(delete(m.rate_events).where(m.rate_events.c.key == key))
