"""The hourly sync with the server's WOM group, and the labels it writes.

- Every membership comes in one request. Compared with what is stored, it gives joins, leaves, rank changes and
  renames of current members.
- An account missing from the group is away for `away_days` before it has left, so a rename's in-between state,
  where clan sync has sent the new name before WOM approves it, costs nobody their role.
- A sync in which more than a tenth of the accounts in the clan, and more than five, go missing is held: it writes
  nothing and tells the admin channel once, since a broken clan sync and a clear-out look the same.
  `wom.accept_sync` lets it through.
- Name changes add past names, and catch merges: WOM keeps the renamed account's ID and deletes the player that held
  the new name, so the plugin moves that account's person and earlier first join onto the survivor.
- A failed fetch writes nothing, so the labels' freshness shows the outage.
"""

import re
from datetime import date, datetime, timedelta

from pydantic import BaseModel
from teabot.sdk import DirectoryUnavailable, Tx

from teabot_wom import Ctx, keys, plugin, wom
from teabot_wom.names import key
from teabot_wom.people import ACCOUNT, SETTLE, Account, all_people, labels_of, notice

ARCHIVED = re.compile(r"^archive\d+$", re.IGNORECASE)
# Name changes are listed newest submitted first, so one approved late can sit behind newer ones.
OVERLAP = timedelta(days=30)
CLAIM_WAIT = timedelta(days=14)
KEEP_REJECTED = timedelta(days=30)
KEEP_LEFT = timedelta(days=365)


class SyncState(BaseModel):
    group_id: int | None
    fetched_at: datetime | None
    held_since: datetime | None
    held_missing: int | None
    accepted_missing: int
    newest_change: datetime | None


STATE = """
SELECT group_id, fetched_at, held_since, held_missing, accepted_missing, newest_change FROM sync_state
WHERE guild_id = $1
"""
SAVE_STATE = """
INSERT INTO sync_state (guild_id, group_id, fetched_at, group_updated_at, newest_change)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (guild_id) DO UPDATE SET group_id = $2, fetched_at = $3, group_updated_at = $4,
    newest_change = coalesce(greatest(sync_state.newest_change, $5), sync_state.newest_change, $5),
    held_since = NULL, held_missing = NULL, accepted_missing = 0
"""
HOLD = """
INSERT INTO sync_state (guild_id, group_id, held_since, held_missing) VALUES ($1, $2, $3, $4)
ON CONFLICT (guild_id) DO UPDATE SET held_since = coalesce(sync_state.held_since, $3), held_missing = $4
"""
INSERT_ACCOUNT = """
INSERT INTO accounts (guild_id, player_id, name, key, type, status, rank, state, joined, first_joined, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, 'in', $8, $8, now())
"""
UPDATE_ACCOUNT = """
UPDATE accounts SET name = $3, key = $4, past_names = $5, type = $6, status = $7, rank = $8, state = 'in',
    away_since = NULL, left_at = NULL, joined = $9, first_joined = $10, updated_at = now()
WHERE guild_id = $1 AND player_id = $2
"""


def earliest(*days: date | None) -> date | None:
    found = [d for d in days if d is not None]
    return min(found) if found else None


async def state_of(ctx: Ctx) -> SyncState | None:
    return await ctx.db.fetchrow(SyncState, STATE, ctx.guild.id)


@plugin.job(every=timedelta(hours=1))
async def sync(ctx: Ctx) -> None:
    """Read the group and bring the accounts, people and labels up to date."""
    group_id = ctx.config.group_id
    if group_id is None:
        return
    api_key = keys.get().api_key
    try:
        fetched = await wom.group(ctx, api_key, group_id)
    except wom.WomUnavailable:
        return
    state = await state_of(ctx)
    stored = {
        a.player_id: a
        for a in await ctx.db.fetch(Account, f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1", ctx.guild.id)
    }
    members = {m.player_id: m for m in fetched.members}
    same_group = state is not None and state.group_id == group_id
    missing = [a for a in stored.values() if a.state == "in" and a.player_id not in members]
    in_clan = sum(1 for a in stored.values() if a.state == "in")
    accepted = state.accepted_missing if state is not None else 0
    if same_group and len(missing) > max(5, in_clan // 10) and len(missing) > accepted:
        await _hold(ctx, group_id, len(missing), in_clan, state)
        return
    back_to = state.newest_change - OVERLAP if state is not None and state.newest_change else None
    try:
        changes = await wom.name_changes(ctx, api_key, group_id, back_to)
    except wom.WomUnavailable:
        return
    async with ctx.db.transaction() as tx:
        for m in fetched.members:
            await _member(tx, ctx, m, stored.get(m.player_id))
        await _leaving(tx, ctx, missing, stored)
        newest = await _name_changes(tx, ctx, changes, set(members))
        await _claims(tx, ctx)
        await tx.execute(SAVE_STATE, ctx.guild.id, group_id, ctx.now, fetched.updated_at, newest)
        await tx.schedule(write_labels.bind(), at=ctx.now + SETTLE)


async def _hold(ctx: Ctx, group_id: int, missing: int, in_clan: int, state: "SyncState | None") -> None:
    async with ctx.db.transaction() as tx:
        await tx.execute(HOLD, ctx.guild.id, group_id, ctx.now, missing)
        channel = ctx.config.admin_channel
        if channel is not None and (state is None or state.held_since is None):
            await tx.send(
                int(channel),
                f"**Wise Old Man sync held.** {missing} of the clan's {in_clan} accounts are missing from the group "
                "at once, which a broken clan sync also looks like. Nothing changed. If they really left, "
                "`/wom accept_sync` takes the group as it is; otherwise a clan sync in game puts them back.",
            )


async def _member(tx: Tx, ctx: Ctx, m: wom.Member, old: Account | None) -> None:
    archived = (m.status or "").lower() == "archived" or bool(ARCHIVED.match(m.name))
    if old is None:
        await tx.execute(
            INSERT_ACCOUNT, ctx.guild.id, m.player_id, m.name, key(m.name), m.type, m.status, m.rank, m.joined
        )
        return
    name, past = old.name, list(old.past_names)
    if archived and (old.status or "").lower() != "archived":
        await notice(tx, ctx.guild.id, "archive", f"{old.name} was archived by WOM; a new ID may hold its name now.")
    elif not archived and key(m.name) != old.key:
        name = m.name
        if old.name not in past:
            past.append(old.name)
    # A stint starts again only after the account has really left; a rename or a short absence keeps it.
    joined = m.joined if old.state == "left" else earliest(old.joined, m.joined)
    first = old.first_joined if old.first_joined_by_admin else earliest(old.first_joined, m.joined)
    await tx.execute(
        UPDATE_ACCOUNT, ctx.guild.id, m.player_id, name, key(name), past, m.type, m.status, m.rank, joined, first
    )


async def _leaving(tx: Tx, ctx: Ctx, missing: list[Account], stored: dict[int, Account]) -> None:
    for a in missing:
        await tx.execute(
            "UPDATE accounts SET state = 'away', away_since = $3 WHERE guild_id = $1 AND player_id = $2",
            ctx.guild.id,
            a.player_id,
            ctx.now,
        )
    gone_before = ctx.now - timedelta(days=ctx.config.away_days)
    await tx.execute(
        "UPDATE accounts SET state = 'left', left_at = $2 WHERE guild_id = $1 AND state = 'away' AND away_since < $3",
        ctx.guild.id,
        ctx.now,
        gone_before,
    )
    await tx.execute(
        "DELETE FROM accounts WHERE guild_id = $1 AND state = 'left' AND person_id IS NULL AND left_at < $2 "
        "AND NOT first_joined_by_admin",
        ctx.guild.id,
        ctx.now - KEEP_LEFT,
    )


async def _name_changes(tx: Tx, ctx: Ctx, changes: list[wom.NameChange], members: set[int]) -> datetime | None:
    newest: datetime | None = None
    for c in sorted(changes, key=lambda c: c.created_at):
        newest = c.created_at if newest is None else max(newest, c.created_at)
        first = await tx.fetchval(
            "INSERT INTO seen_changes (guild_id, change_id) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING 1",
            ctx.guild.id,
            c.id,
        )
        if first is None:
            continue
        survivor = await tx.fetchrow(
            Account, f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1 AND player_id = $2", ctx.guild.id, c.player_id
        )
        if survivor is None:
            continue
        if c.old_name not in survivor.past_names and key(c.old_name) != survivor.key:
            await tx.execute(
                "UPDATE accounts SET past_names = array_append(past_names, $3) WHERE guild_id = $1 AND player_id = $2",
                ctx.guild.id,
                c.player_id,
                c.old_name,
            )
        await _merge(tx, ctx, survivor, c, members)
    return newest


async def _merge(tx: Tx, ctx: Ctx, survivor: Account, c: wom.NameChange, members: set[int]) -> None:
    """A rename approved for a name WOM already tracked merges the two players, keeping the renamed one's ID."""
    vanished = await tx.fetchrow(
        Account,
        f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1 AND key = $2 AND player_id <> $3",
        ctx.guild.id,
        key(c.new_name),
        survivor.player_id,
    )
    if vanished is None or vanished.player_id in members:
        return
    person = survivor.person_id or vanished.person_id
    if vanished.first_joined_by_admin and not survivor.first_joined_by_admin:
        first, by_admin = vanished.first_joined, True
    else:
        first, by_admin = earliest(survivor.first_joined, vanished.first_joined), survivor.first_joined_by_admin
    await tx.execute("DELETE FROM accounts WHERE guild_id = $1 AND player_id = $2", ctx.guild.id, vanished.player_id)
    await tx.execute(
        "UPDATE accounts SET person_id = $3, first_joined = $4, first_joined_by_admin = $5 "
        "WHERE guild_id = $1 AND player_id = $2",
        ctx.guild.id,
        survivor.player_id,
        person,
        first,
        by_admin,
    )
    await notice(tx, ctx.guild.id, "merge", f"WOM merged {vanished.name} into {survivor.name} (renamed {c.new_name}).")


async def _claims(tx: Tx, ctx: Ctx) -> None:
    """Expire waiting claims on accounts that never joined, and forget settled ones after a while."""
    await tx.execute(
        "DELETE FROM claims WHERE guild_id = $1 AND state = 'waiting' AND created_at < $2 AND NOT EXISTS "
        "(SELECT 1 FROM accounts a WHERE a.guild_id = claims.guild_id AND a.key = claims.key)",
        ctx.guild.id,
        ctx.now - CLAIM_WAIT,
    )
    await tx.execute(
        "DELETE FROM claims WHERE guild_id = $1 AND state <> 'waiting' AND resolved_at < $2",
        ctx.guild.id,
        ctx.now - KEEP_REJECTED,
    )


@plugin.job
async def write_labels(ctx: Ctx) -> None:
    """Write every linked member's labels, as fresh as the last good fetch."""
    state = await state_of(ctx)
    if state is None or state.fetched_at is None:
        return
    people, owned = await all_people(ctx.db, ctx.guild.id)
    labels = {p.user_id: labels_of(owned.get(p.id, [])) for p in people if p.user_id is not None}
    try:
        await ctx.labels.replace_all(labels, as_of=state.fetched_at)
    except DirectoryUnavailable:
        return


async def relabel(tx: Tx, ctx: Ctx) -> None:
    """Write the labels again a moment after an admin's change, so a burst of changes makes one write."""
    await tx.schedule(write_labels.bind(), at=ctx.now + SETTLE)
