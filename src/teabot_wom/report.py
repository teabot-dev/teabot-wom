"""What admins read: the waiting claims with their evidence, the people, the gaps with the rank audit, and the
weekly digest.

Names are member-written or player-chosen text, so outputs carry them as `UntrustedText`.
"""

from collections import Counter
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from teabot.sdk import READ, DirectoryMember, MemberFilter, MemberRef, UntrustedText

from teabot_wom import Ctx, plugin
from teabot_wom.names import contains, key
from teabot_wom.people import ACCOUNT, Account, Person, all_people, expected_rank, ladder, main_of, since_of

SHOWN = 25


class Evidence(BaseModel):
    id: int = Field(description="The claim's number, for wom.confirm and wom.reject")
    member: MemberRef
    name: UntrustedText
    in_group: bool
    rank: str | None
    joined: date | None
    matches: list[str] = Field(description="The member's Discord names that match the account's, current or past")
    competing: bool = Field(description="Another member claims it too, or it is already someone else's")


class Suggestion(BaseModel):
    member: MemberRef
    name: UntrustedText = Field(description="The account's current name")
    discord_name: UntrustedText = Field(description="The member's Discord name that matched")
    matched: UntrustedText = Field(description="The account name it matched, current or past")
    past_name: bool = Field(description="Whether it matched a name the account no longer has")


class ClaimsPage(BaseModel):
    claims: list[Evidence]
    suggestions: list[Suggestion] = Field(description="Unlinked accounts a member's Discord name matches; no claim")


class Waiting(BaseModel):
    id: int
    user_id: int
    name: str
    key: str


async def _directory(ctx: Ctx) -> dict[int, DirectoryMember]:
    found: dict[int, DirectoryMember] = {}
    after = 0
    while True:
        page = await ctx.members.find(MemberFilter(bots=False), after=after)
        if not page:
            return found
        found |= {m.user_id: m for m in page}
        after = page[-1].user_id


def _matches(member: DirectoryMember | None, account: Account | None, name: str) -> list[str]:
    if member is None:
        return []
    wanted = [name, *(account.past_names if account else [])]
    mine = [n for n in (member.nickname, member.display_name, member.name) if n]
    return sorted({n for n in mine for w in wanted if key(n) == key(w) or contains(n, w)})


def _first_match(member: DirectoryMember, account: Account) -> tuple[str, str] | None:
    """The member's first Discord name that matches the account, and which of its names it matched."""
    for n in (member.nickname, member.display_name, member.name):
        for w in (account.name, *account.past_names):
            if n and (key(n) == key(w) or contains(n, w)):
                return n, w
    return None


@plugin.action(risk=READ, needs="manage_guild")
async def claims(ctx: Ctx) -> ClaimsPage:
    """Members' claims waiting for an admin, with their evidence, and suggested links."""
    waiting = await ctx.db.fetch(
        Waiting,
        "SELECT id, user_id, name, key FROM claims WHERE guild_id = $1 AND state = 'waiting' ORDER BY id",
        ctx.guild.id,
    )
    accounts = {
        a.key: a
        for a in await ctx.db.fetch(Account, f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1", ctx.guild.id)
    }
    people, _owned = await all_people(ctx.db, ctx.guild.id)
    member_of = {p.id: p.user_id for p in people}
    claimed = Counter(w.key for w in waiting)
    members = await _directory(ctx)
    evidence: list[Evidence] = []
    for w in waiting[:SHOWN]:
        account = accounts.get(w.key)
        owner = member_of.get(account.person_id) if account and account.person_id else None
        evidence.append(
            Evidence(
                id=w.id,
                member=w.user_id,
                name=w.name,
                in_group=account is not None and account.present,
                rank=account.rank if account else None,
                joined=account.joined if account else None,
                matches=_matches(members.get(w.user_id), account, w.name),
                competing=claimed[w.key] > 1 or (owner is not None and owner != w.user_id),
            )
        )
    linked = {p.user_id for p in people if p.user_id is not None} | {w.user_id for w in waiting}
    suggestions: list[Suggestion] = []
    free = [a for a in accounts.values() if a.present and a.person_id is None and a.key not in claimed]
    for m in members.values():
        if m.user_id in linked or len(suggestions) >= SHOWN:
            continue
        for a in free:
            if found := _first_match(m, a):
                discord_name, matched = found
                suggestions.append(
                    Suggestion(
                        member=m.user_id,
                        name=a.name,
                        discord_name=discord_name,
                        matched=matched,
                        past_name=matched != a.name,
                    )
                )
                break
    return ClaimsPage(claims=evidence, suggestions=suggestions)


class PersonEntry(BaseModel):
    id: int = Field(description="The person's number, for wom.merge")
    member: MemberRef | None
    note: UntrustedText | None
    main: UntrustedText | None
    accounts: list[UntrustedText]
    since: date | None = Field(description="When they joined, as the rank rule counts it")


class PeoplePage(BaseModel):
    people: list[PersonEntry]
    total: int


type Which = Literal["all", "no_member", "in_clan", "away", "left"]


def _entry(ctx: Ctx, p: Person, accounts: list[Account]) -> PersonEntry:
    main = main_of(p, accounts)
    return PersonEntry(
        id=p.id,
        member=p.user_id,
        note=p.note,
        main=main.name if main else None,
        accounts=[a.name for a in accounts],
        since=since_of(ctx.config.rank_rule, accounts),
    )


@plugin.action(risk=READ, needs="manage_guild")
async def people(ctx: Ctx, which: Which = "all", page: int = 1) -> PeoplePage:
    """People, their accounts, main and join date.

    Args:
        which: Everyone, those with no member, in the clan, away, or left
        page: Which page of 50, from 1
    """
    found, owned = await all_people(ctx.db, ctx.guild.id)

    def wanted(p: Person) -> bool:
        accounts = owned.get(p.id, [])
        states = {a.state for a in accounts}
        return {
            "all": True,
            "no_member": p.user_id is None,
            "in_clan": "in" in states,
            "away": "away" in states,
            "left": bool(accounts) and not states & {"in", "away"},
        }[which]

    chosen = [p for p in found if wanted(p)]
    start = (max(page, 1) - 1) * 50
    return PeoplePage(
        people=[_entry(ctx, p, owned.get(p.id, [])) for p in chosen[start : start + 50]], total=len(chosen)
    )


class Gap(BaseModel):
    kind: str
    count: int
    examples: list[UntrustedText]


class GapsPage(BaseModel):
    gaps: list[Gap]
    last_sync: str | None = Field(description="When teabot last read the group; none before the first sync")
    last_change: str | None = Field(description="When the group last changed, the nearest WOM says to its last sync")


async def gaps_of(ctx: Ctx) -> GapsPage:
    accounts = await ctx.db.fetch(
        Account, f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1 ORDER BY name", ctx.guild.id
    )
    found, owned = await all_people(ctx.db, ctx.guild.id)
    out: dict[str, list[str]] = {}

    def add(kind: str, text: str) -> None:
        out.setdefault(kind, []).append(text)

    for a in accounts:
        if a.present and a.person_id is None:
            add("clan accounts with no member", a.name)
        if a.present and a.joined is None:
            add("missing join dates", a.name)
        if (a.status or "").lower() == "banned":
            add("banned accounts", a.name)
    waiting = await ctx.db.fetch(
        Waiting, "SELECT id, user_id, name, key FROM claims WHERE guild_id = $1 AND state = 'waiting'", ctx.guild.id
    )
    counted = Counter(w.key for w in waiting)
    owner_key = {a.key for a in accounts if a.person_id is not None}
    for w in waiting:
        if counted[w.key] > 1 or w.key in owner_key:
            add("competing claims", w.name)
    if ctx.config.clan_role is not None:
        linked = {p.user_id for p in found if p.user_id is not None and owned.get(p.id)}
        holders = await ctx.members.find(MemberFilter(roles_any=[int(ctx.config.clan_role)], bots=False))
        for m in holders:
            if m.user_id not in linked:
                add("clan role holders with no confirmed account", m.display_name)
    for n in await ctx.db.fetch(Notice, NOTICES, ctx.guild.id, ctx.now - timedelta(days=30)):
        add(f"{n.kind}s", n.text)
    rule = ctx.config.rank_rule
    if rule is not None and rule.ranks:
        order = ladder(rule)
        governed = set(order)
        today = ctx.now.date()
        for p in found:
            mine = owned.get(p.id, [])
            since = since_of(rule, mine)
            for a in mine:
                if not a.present or a.rank not in governed:
                    continue
                if since is None:
                    add("ranks with no join date to check", a.name)
                elif (want := expected_rank(rule, since, today)) is not None and order.index(want) > order.index(
                    str(a.rank)
                ):
                    add("in-game ranks to promote", f"{a.name}: {a.rank} → {want}")
        waiting_keys = {w.key: w for w in waiting}
        for a in accounts:
            if a.present and a.person_id is None and a.key in waiting_keys and a.rank in governed:
                add("unconfirmed claims the audit skips", a.name)
    times = await ctx.db.fetchrow(
        "SELECT to_char(fetched_at, 'YYYY-MM-DD HH24:MI') AS synced, to_char(group_updated_at, 'YYYY-MM-DD HH24:MI') "
        "AS changed FROM sync_state WHERE guild_id = $1",
        ctx.guild.id,
    )
    return GapsPage(
        gaps=[Gap(kind=k, count=len(v), examples=v[:SHOWN]) for k, v in out.items()],
        last_sync=times["synced"] if times else None,
        last_change=times["changed"] if times else None,
    )


class Notice(BaseModel):
    kind: str
    text: str


NOTICES = "SELECT kind, text FROM notices WHERE guild_id = $1 AND at > $2 ORDER BY at"


@plugin.action(risk=READ, needs="manage_guild")
async def gaps(ctx: Ctx) -> GapsPage:
    """What needs a person: unlinked clan accounts, competing claims, merges, archives, missing join dates, and the
    in-game ranks the rank rule calls for."""
    return await gaps_of(ctx)


@plugin.job(every=timedelta(days=7))
async def digest(ctx: Ctx) -> None:
    """Post the week's gaps and waiting claims to the admin channel."""
    channel = ctx.config.admin_channel
    if channel is None or ctx.config.group_id is None:
        return
    found = await gaps_of(ctx)
    waiting = await ctx.db.fetchval(
        int, "SELECT count(*) FROM claims WHERE guild_id = $1 AND state = 'waiting'", ctx.guild.id
    )
    lines = ["**Wise Old Man this week**", f"- {waiting} claims waiting: `/wom claims`"]
    lines += [f"- {g.count} {g.kind}" for g in found.gaps]
    async with ctx.db.transaction() as tx:
        await tx.send(int(channel), "\n".join(lines))
        await tx.execute(
            "DELETE FROM notices WHERE guild_id = $1 AND at < $2", ctx.guild.id, ctx.now - timedelta(days=30)
        )
