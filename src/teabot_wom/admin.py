"""What admins do: confirm and reject claims, link and unlink accounts by hand, merge and split people, pin a main,
set join dates, and let a held sync through.

Every action that changes who holds which account changes labels, which role rules turn into Discord roles, so it
is destructive: its plan lists each person's labels before and after, and an assistant on a default token can't
make it.
"""

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field
from teabot.sdk import DESTRUCTIVE, ActionError, Diff, DiffEntry, Member

from teabot_wom import Ctx, keys, plugin, wom
from teabot_wom.names import NAME_MAX, key, names_in
from teabot_wom.people import (
    Account,
    account_named,
    accounts_of,
    change_text,
    ensure_person,
    person_by_id,
    person_of,
)
from teabot_wom.people import (
    merge as merge_people,
)
from teabot_wom.sync import relabel, state_of

DAY = r"^\d{4}-\d{2}-\d{2}$"
type Name = Annotated[str, Field(max_length=NAME_MAX)]
type Claims = Annotated[list[int], Field(max_length=10)]


class Claim(BaseModel):
    id: int
    user_id: int
    name: str
    key: str


CLAIM = "SELECT id, user_id, name, key FROM claims WHERE guild_id = $1 AND id = $2 AND state = 'waiting'"


async def _confirm_entry(ctx: Ctx, claim_id: int) -> DiffEntry:
    found = await ctx.db.fetchrow(Claim, CLAIM, ctx.guild.id, claim_id)
    if found is None:
        return DiffEntry(change="skip", target=f"claim {claim_id}", detail="no longer waiting", key=claim_id)
    target = f"<@{found.user_id}>: {found.name}"
    account = await account_named(ctx.db, ctx.guild.id, found.name)
    if account is None or account.state == "outside":
        return DiffEntry(change="skip", target=target, detail="not in the clan's group yet", key=claim_id)
    mine = await person_of(ctx.db, ctx.guild.id, found.user_id)
    before = await accounts_of(ctx.db, ctx.guild.id, mine.id) if mine else []
    after = [*before, account]
    if account.person_id is not None and (mine is None or account.person_id != mine.id):
        other = await person_by_id(ctx.db, ctx.guild.id, account.person_id)
        if other is not None and other.user_id is not None:
            return DiffEntry(change="skip", target=target, detail="belongs to another member", key=claim_id)
        after = [*before, *await accounts_of(ctx.db, ctx.guild.id, account.person_id)]
    return DiffEntry(change="confirm", target=target, detail=change_text(before, after), key=claim_id)


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def confirm(ctx: Ctx, claims: Claims) -> str:
    """Confirm members' claims on their accounts, as wom.claims lists them.

    Args:
        claims: The claims' numbers, up to ten
    """
    done = 0
    async with ctx.db.transaction() as tx:
        for entry in ctx.diff.entries:
            if entry.change != "confirm" or not isinstance(entry.key, int):
                continue
            found = await tx.fetchrow(Claim, CLAIM, ctx.guild.id, entry.key)
            account = await account_named(tx, ctx.guild.id, found.name) if found else None
            if found is None or account is None:
                continue
            person = await ensure_person(tx, ctx.guild.id, found.user_id)
            if account.person_id is not None and account.person_id != person:
                other = await person_by_id(tx, ctx.guild.id, account.person_id)
                if other is None or other.user_id is not None:
                    continue
                await merge_people(tx, ctx.guild.id, person, other.id)
            await tx.execute(
                "UPDATE accounts SET person_id = $3 WHERE guild_id = $1 AND player_id = $2",
                ctx.guild.id,
                account.player_id,
                person,
            )
            await tx.execute(
                "UPDATE claims SET state = 'confirmed', resolved_at = $3 WHERE guild_id = $1 AND id = $2",
                ctx.guild.id,
                found.id,
                ctx.now,
            )
            done += 1
        await relabel(tx, ctx)
    return f"Confirmed {done}."


@confirm.plan
async def plan_confirm(ctx: Ctx, claims: Claims) -> Diff:
    return Diff(entries=[await _confirm_entry(ctx, c) for c in claims])


@plugin.action(needs="manage_guild")
async def reject(ctx: Ctx, claims: Claims) -> str:
    """Reject members' claims, as wom.claims lists them.

    Args:
        claims: The claims' numbers, up to ten
    """
    status = await ctx.db.execute(
        "UPDATE claims SET state = 'rejected', resolved_at = $3 WHERE guild_id = $1 AND id = ANY($2::bigint[]) "
        "AND state = 'waiting'",
        ctx.guild.id,
        claims,
        ctx.now,
    )
    return f"Rejected {status.split()[-1]}."


async def _target(ctx: Ctx, member: Member | None, note: str) -> tuple[int | None, list[Account], str]:
    """The person an admin's link goes to, their accounts, and how a plan names them."""
    if member is not None:
        person = await person_of(ctx.db, ctx.guild.id, member.id)
        accounts = await accounts_of(ctx.db, ctx.guild.id, person.id) if person else []
        return (person.id if person else None), accounts, f"<@{member.id}>"
    if not note:
        raise ActionError("Give the member, or a note naming someone who isn't on Discord.")
    found = await ctx.db.fetchval("SELECT id FROM people WHERE guild_id = $1 AND note = $2", ctx.guild.id, note)
    return found, (await accounts_of(ctx.db, ctx.guild.id, found) if found else []), note


async def _account_or_player(ctx: Ctx, name: str) -> Account | wom.Member:
    found = await account_named(ctx.db, ctx.guild.id, name)
    if found is not None:
        return found
    player = await wom.player(ctx, keys.get().api_key, name)
    if player is None:
        raise ActionError(f"Neither the clan's group nor Wise Old Man has an account named {name}.")
    return player


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def link(
    ctx: Ctx,
    name: Name,
    member: Member | None = None,
    note: Annotated[str, Field(max_length=100)] = "",
    first_joined: Annotated[str | None, Field(pattern=DAY)] = None,
) -> str:
    """Put an account in a person by hand, such as from a spreadsheet: a member, or someone not on Discord.

    Args:
        name: The account's name
        member: Whose it is
        note: Or a name for someone not on Discord
        first_joined: When they first joined the clan, as YYYY-MM-DD, if known
    """
    found = await _account_or_player(ctx, name)
    async with ctx.db.transaction() as tx:
        if isinstance(found, wom.Member):
            await tx.execute(
                "INSERT INTO accounts (guild_id, player_id, name, key, type, status, state) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'outside') ON CONFLICT DO NOTHING",
                ctx.guild.id,
                found.player_id,
                found.name,
                key(found.name),
                found.type,
                found.status,
            )
        if member is not None:
            person = await ensure_person(tx, ctx.guild.id, member.id)
        else:
            person = await tx.fetchval("SELECT id FROM people WHERE guild_id = $1 AND note = $2", ctx.guild.id, note)
            if person is None:
                person = await tx.fetchval(
                    int, "INSERT INTO people (guild_id, note) VALUES ($1, $2) RETURNING id", ctx.guild.id, note
                )
        await tx.execute(
            "UPDATE accounts SET person_id = $3, first_joined = coalesce($4, first_joined), "
            "first_joined_by_admin = first_joined_by_admin OR $4 IS NOT NULL WHERE guild_id = $1 AND player_id = $2 "
            "AND (person_id IS NULL OR person_id = $3)",
            ctx.guild.id,
            found.player_id,
            person,
            date.fromisoformat(first_joined) if first_joined else None,
        )
        await relabel(tx, ctx)
    return f"Linked {found.name}."


@link.plan
async def plan_link(
    ctx: Ctx, name: Name, member: Member | None = None, note: str = "", first_joined: str | None = None
) -> Diff:
    found = await _account_or_player(ctx, name)
    person, before, who = await _target(ctx, member, note)
    if isinstance(found, Account) and found.person_id is not None and found.person_id != person:
        raise ActionError(f"{found.name} belongs to another person; wom.merge moves accounts between people.")
    added = found if isinstance(found, Account) else None
    after = [*before, added] if added is not None else before
    since = f", first joined {first_joined}" if first_joined else ""
    return Diff(
        entries=[DiffEntry(change="link", target=f"{who}: {found.name}", detail=change_text(before, after) + since)]
    )


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def unlink(ctx: Ctx, name: Name) -> str:
    """Take an account out of its person.

    Args:
        name: The account's name
    """
    async with ctx.db.transaction() as tx:
        await tx.execute(
            "UPDATE accounts SET person_id = NULL WHERE guild_id = $1 AND key = $2", ctx.guild.id, key(name)
        )
        await relabel(tx, ctx)
    return f"Unlinked {name}."


@unlink.plan
async def plan_unlink(ctx: Ctx, name: Name) -> Diff:
    found = await account_named(ctx.db, ctx.guild.id, name)
    if found is None or found.person_id is None:
        raise ActionError(f"{name} is in no person.")
    before = await accounts_of(ctx.db, ctx.guild.id, found.person_id)
    after = [a for a in before if a.player_id != found.player_id]
    return Diff(entries=[DiffEntry(change="unlink", target=found.name, detail=change_text(before, after))])


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def forget(ctx: Ctx, member: Member) -> str:
    """Forget a member's person, such as one linked by mistake: their links and waiting claims go, so role rules
    leave their roles alone.

    Args:
        member: The member
    """
    async with ctx.db.transaction() as tx:
        await tx.execute("DELETE FROM people WHERE guild_id = $1 AND user_id = $2", ctx.guild.id, member.id)
        await tx.execute(
            "DELETE FROM claims WHERE guild_id = $1 AND user_id = $2 AND state = 'waiting'", ctx.guild.id, member.id
        )
        await relabel(tx, ctx)
    return f"Forgot {member.display_name}."


@forget.plan
async def plan_forget(ctx: Ctx, member: Member) -> Diff:
    person = await person_of(ctx.db, ctx.guild.id, member.id)
    if person is None:
        raise ActionError(f"{member.display_name} has no person.")
    accounts = await accounts_of(ctx.db, ctx.guild.id, person.id)
    names = ", ".join(a.name for a in accounts) or "no accounts"
    return Diff(entries=[DiffEntry(change="forget", target=f"<@{member.id}>", detail=f"{names}; labels all removed")])


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def merge(ctx: Ctx, into: Member, person: int) -> str:
    """Move every account of a person not on Discord, as wom.people numbers them, into a member's person.

    Args:
        into: The member
        person: The other person's number
    """
    async with ctx.db.transaction() as tx:
        target = await ensure_person(tx, ctx.guild.id, into.id)
        await merge_people(tx, ctx.guild.id, target, person)
        await relabel(tx, ctx)
    return "Merged."


@merge.plan
async def plan_merge(ctx: Ctx, into: Member, person: int) -> Diff:
    other = await person_by_id(ctx.db, ctx.guild.id, person)
    if other is None:
        raise ActionError(f"There is no person {person}.")
    if other.user_id is not None:
        raise ActionError("That person has a member of their own, so they can't be merged into another.")
    mine = await person_of(ctx.db, ctx.guild.id, into.id)
    before = await accounts_of(ctx.db, ctx.guild.id, mine.id) if mine else []
    after = [*before, *await accounts_of(ctx.db, ctx.guild.id, person)]
    return Diff(
        entries=[DiffEntry(change="merge", target=f"{other.note} into <@{into.id}>", detail=change_text(before, after))]
    )


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def split(
    ctx: Ctx, names: Annotated[str, Field(max_length=100)], note: Annotated[str, Field(max_length=100)]
) -> str:
    """Move accounts out of their person into a new one, for someone not on Discord.

    Args:
        names: The accounts, separated by commas
        note: A name for the new person
    """
    async with ctx.db.transaction() as tx:
        made = await tx.fetchval(
            int, "INSERT INTO people (guild_id, note) VALUES ($1, $2) RETURNING id", ctx.guild.id, note
        )
        for name in names_in(names):
            await tx.execute(
                "UPDATE accounts SET person_id = $3 WHERE guild_id = $1 AND key = $2 AND person_id IS NOT NULL",
                ctx.guild.id,
                key(name),
                made,
            )
        await relabel(tx, ctx)
    return f"Moved them to {note}."


@split.plan
async def plan_split(ctx: Ctx, names: str, note: str) -> Diff:
    entries: list[DiffEntry] = []
    for name in names_in(names):
        found = await account_named(ctx.db, ctx.guild.id, name)
        if found is None or found.person_id is None:
            raise ActionError(f"{name} is in no person.")
        before = await accounts_of(ctx.db, ctx.guild.id, found.person_id)
        after = [a for a in before if a.player_id != found.player_id]
        entries.append(DiffEntry(change="move", target=f"{found.name} to {note}", detail=change_text(before, after)))
    return Diff(entries=entries)


@plugin.action(needs="manage_guild")
async def pin_main(ctx: Ctx, name: Name) -> str:
    """Make an account its person's main, which their nickname shows and the rank audit counts from.

    Args:
        name: The account's name
    """
    found = await account_named(ctx.db, ctx.guild.id, name)
    if found is None or found.person_id is None:
        raise ActionError(f"{name} is in no person.")
    await ctx.db.execute(
        "UPDATE people SET pinned_main = $3 WHERE guild_id = $1 AND id = $2",
        ctx.guild.id,
        found.person_id,
        found.player_id,
    )
    return f"{found.name} is now its person's main."


@plugin.action(needs="manage_guild")
async def set_joined(
    ctx: Ctx,
    name: Name,
    joined: Annotated[str | None, Field(pattern=DAY)] = None,
    first_joined: Annotated[str | None, Field(pattern=DAY)] = None,
) -> str:
    """Set an account's join dates by hand, such as one Wise Old Man doesn't know, or a wrong import.

    Args:
        name: The account's name
        joined: The current stint's join date, as YYYY-MM-DD
        first_joined: The first join ever, as YYYY-MM-DD
    """
    found = await account_named(ctx.db, ctx.guild.id, name)
    if found is None:
        raise ActionError(f"There is no account named {name}.")
    await ctx.db.execute(
        "UPDATE accounts SET joined = coalesce($3, joined), first_joined = coalesce($4, first_joined), "
        "first_joined_by_admin = first_joined_by_admin OR $4 IS NOT NULL WHERE guild_id = $1 AND player_id = $2",
        ctx.guild.id,
        found.player_id,
        date.fromisoformat(joined) if joined else None,
        date.fromisoformat(first_joined) if first_joined else None,
    )
    return f"Set {found.name}'s dates."


@plugin.action(risk=DESTRUCTIVE, needs="manage_guild")
async def accept_sync(ctx: Ctx) -> str:
    """Let a held sync through: the accounts missing from the group really left."""
    held = await state_of(ctx)
    async with ctx.db.transaction() as tx:
        await tx.execute(
            "UPDATE sync_state SET accepted_missing = $2 WHERE guild_id = $1",
            ctx.guild.id,
            len(ctx.diff.entries) if held is not None else 0,
        )
        from teabot_wom.sync import sync

        await tx.schedule(sync.bind())
    return "The next sync takes the group as it is."


@accept_sync.plan
async def plan_accept_sync(ctx: Ctx) -> Diff:
    held = await state_of(ctx)
    if held is None or held.held_since is None or ctx.config.group_id is None:
        raise ActionError("No sync is held.")
    fetched = await wom.group(ctx, keys.get().api_key, ctx.config.group_id)
    present = {m.player_id for m in fetched.members}
    gone = await ctx.db.fetch(
        Account,
        "SELECT player_id, name, key, past_names, type, status, rank, state, away_since, left_at, joined, "
        "first_joined, first_joined_by_admin, person_id FROM accounts WHERE guild_id = $1 AND state = 'in' "
        "ORDER BY name",
        ctx.guild.id,
    )
    leaving = [a for a in gone if a.player_id not in present]
    return Diff(
        notes=[f"{len(leaving)} accounts count as away from the next sync, and as left after the grace period."],
        entries=[DiffEntry(change="away", target=a.name, key=a.player_id) for a in leaving],
    )
