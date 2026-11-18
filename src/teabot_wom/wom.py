"""Wise Old Man's API: a group's memberships and its name changes, read through `ctx.http`.

Requests are paced per instance, under WOM's limit of 20 a minute without a key and 100 with one, and send a
User-Agent naming the project, as WOM asks, with nothing about the operator.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, SecretStr
from teabot.sdk import ActionError, Context

BASE = "https://api.wiseoldman.net/v2"
AGENT = "teabot-wom/1.0 (+https://github.com/teabot-dev/teabot-wom)"
NAME_CHANGE_FORM = "https://wiseoldman.net/names/submit"
PAGE = 50
# Seconds between requests from this instance: under 20 a minute without a key, under 100 with one.
GAP_WITHOUT_KEY = 3.2
GAP_WITH_KEY = 0.7


class Keys(BaseModel):
    """`TEABOT_PLUGIN_WOM_API_KEY`: a key from WOM's Discord, which raises its limits, for an instance with many
    servers."""

    api_key: SecretStr | None = None


class WomUnavailable(ActionError):
    """WOM did not answer, or answered with an error; the message says which."""


@dataclass(frozen=True)
class Member:
    """One membership of the group, as WOM reports it."""

    player_id: int
    name: str
    type: str | None
    status: str | None
    rank: str | None
    joined: date | None


@dataclass(frozen=True)
class Group:
    name: str
    updated_at: datetime | None
    members: list[Member]


@dataclass(frozen=True)
class NameChange:
    id: int
    player_id: int
    old_name: str
    new_name: str
    created_at: datetime


class Pacer:
    """One request at a time from this instance, spaced by the gap its key allows."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.gap: float | None = None

    async def wait(self, has_key: bool) -> None:
        loop = asyncio.get_running_loop()
        gap = self.gap if self.gap is not None else (GAP_WITH_KEY if has_key else GAP_WITHOUT_KEY)
        delay = self.last + gap - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self.last = loop.time()


pacer = Pacer()


async def _get(ctx: Context[Any], key: SecretStr | None, path: str, params: dict[str, Any] | None = None) -> Any:
    headers = {"User-Agent": AGENT}
    if key is not None:
        headers["x-api-key"] = key.get_secret_value()
    async with pacer.lock:
        await pacer.wait(key is not None)
        reply = await ctx.http.get(f"{BASE}{path}", params=params, headers=headers, wait=30)
    if not reply.ok:
        raise WomUnavailable(f"Wise Old Man answered {reply.status} for {path}.")
    return reply.json()


def _day(value: Any) -> date | None:
    return date.fromisoformat(str(value)[:10]) if value else None


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


async def group(ctx: Context[Any], key: SecretStr | None, group_id: int) -> Group:
    """The group's every membership, in one request."""
    body = await _get(ctx, key, f"/groups/{group_id}")
    members = [
        Member(
            player_id=int(m["playerId"]),
            name=str(m["player"]["displayName"]),
            type=m["player"].get("type"),
            status=m["player"].get("status"),
            rank=m.get("role"),
            joined=_day(m.get("clientSyncJoinedAt")),
        )
        for m in body.get("memberships", [])
    ]
    return Group(str(body.get("name", "")), _time(body.get("updatedAt")), members)


async def name_changes(
    ctx: Context[Any], key: SecretStr | None, group_id: int, back_to: datetime | None, most: int = 40
) -> list[NameChange]:
    """The group's approved name changes, newest submitted first, paging until a page is older than `back_to`."""
    found: list[NameChange] = []
    for n in range(most):
        body = await _get(ctx, key, f"/groups/{group_id}/name-changes", {"limit": PAGE, "offset": n * PAGE})
        page = [
            NameChange(int(c["id"]), int(c["playerId"]), str(c["oldName"]), str(c["newName"]), created)
            for c in body
            if c.get("status") == "approved" and (created := _time(c.get("createdAt"))) is not None
        ]
        found += page
        if len(body) < PAGE or (back_to is not None and page and all(c.created_at < back_to for c in page)):
            break
    return found


async def player(ctx: Context[Any], key: SecretStr | None, name: str) -> Member | None:
    """A player by name, for an admin linking an account outside the group, or None when WOM has none."""
    try:
        body = await _get(ctx, key, f"/players/{name}")
    except WomUnavailable:
        return None
    return Member(int(body["id"]), str(body["displayName"]), body.get("type"), body.get("status"), None, None)
