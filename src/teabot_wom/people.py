"""People and their accounts: who a person's main is, the labels they give, and the queries the actions share.

A person's labels come from their confirmed accounts only:
- `linked` for every person with a member, accounts or not, so a member who drops every account stays inside a role
  rule's scope;
- `in_clan` when an account is in the clan or away, and `left` when they have accounts and none is;
- `rank:<rank>` for each in-game rank one of their accounts in the clan, or away, holds, where an inactive rank
  counts only when every such account holds one;
- `tenure:<rank>`, with a `rank_rule` set, while one of their accounts is in the clan: the rank the rule gives for
  their days in it, or a higher rank of the rule that one of those accounts holds, since a member ranked up early
  keeps it.
"""

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from pydantic import BaseModel
from teabot.sdk import Queries, Tx

from teabot_wom import Ctx, RankRule, WomConfig
from teabot_wom.names import key

PRESENT = ("in", "away")
ACCOUNT = """
player_id, name, key, past_names, type, status, rank, state, away_since, left_at, joined, first_joined,
first_joined_by_admin, person_id
"""
# Labels are written a moment after the change, so a burst of changes, such as an import, makes one write.
SETTLE = timedelta(minutes=1)


class Account(BaseModel):
    player_id: int
    name: str
    key: str
    past_names: list[str]
    type: str | None
    status: str | None
    rank: str | None
    state: str
    away_since: datetime | None
    left_at: datetime | None
    joined: date | None
    first_joined: date | None
    first_joined_by_admin: bool
    person_id: int | None

    @property
    def present(self) -> bool:
        return self.state in PRESENT


class Person(BaseModel):
    id: int
    user_id: int | None
    note: str | None
    pinned_main: int | None


def main_of(person: Person, accounts: Sequence[Account]) -> Account | None:
    """The person's main: the account an admin pinned, else, among their accounts in the clan or away, or all of
    them when none is, the earliest first joined, then the earliest joined, then the lowest WOM ID."""
    pinned = next((a for a in accounts if a.player_id == person.pinned_main), None)
    if pinned is not None:
        return pinned
    pool = [a for a in accounts if a.present] or list(accounts)

    def order(a: Account) -> tuple[bool, date, bool, date, int]:
        return (a.first_joined is None, a.first_joined or date.max, a.joined is None, a.joined or date.max, a.player_id)

    return min(pool, key=order, default=None)


def since_of(rule: RankRule | None, accounts: Iterable[Account]) -> date | None:
    """When a person joined, as the rank rule counts it: their first join ever, or the earliest among accounts in
    the clan or away."""
    found = list(accounts)
    if rule is not None and rule.count_from == "first_joined":
        days = [a.first_joined for a in found if a.first_joined]
    else:
        days = [a.joined for a in found if a.present and a.joined]
    return min(days, default=None)


def expected_rank(rule: RankRule, since: date, today: date) -> str | None:
    """The rank the rule gives for this many days in the clan: the step with the most days not above them."""
    days = (today - since).days
    reached = [s for s in sorted(rule.ranks, key=lambda s: s.days) if s.days <= days]
    return reached[-1].rank if reached else None


def ladder(rule: RankRule) -> list[str]:
    """The rule's ranks, lowest first."""
    return [s.rank for s in sorted(rule.ranks, key=lambda s: s.days)]


def tenure_of(rule: RankRule, accounts: Iterable[Account], today: date) -> str | None:
    """A person's rank on the rule: the one their days give, or a higher one of the rule's that an account of theirs
    in the clan holds."""
    found = list(accounts)
    present = [a for a in found if a.present]
    order = ladder(rule)
    places = [order.index(a.rank) for a in present if a.rank in order]
    since = since_of(rule, found) if present else None
    if since is not None and (due := expected_rank(rule, since, today)) is not None:
        places.append(order.index(due))
    return order[max(places)] if places else None


def label(rank: str) -> str:
    return rank.replace(" ", "_")


def labels_of(accounts: Iterable[Account], config: WomConfig, today: date) -> list[str]:
    """The labels a person with a member gives, from their confirmed accounts and the clan's settings."""
    found = list(accounts)
    rule = config.rank_rule
    out = {"linked"}
    if found:
        out.add("in_clan" if any(a.present for a in found) else "left")
    ranks = {a.rank for a in found if a.present and a.rank}
    if active := ranks - set(config.inactive_ranks):
        # A person with an active account isn't inactive, whatever their other accounts hold.
        ranks = active
    out |= {f"rank:{label(r)}" for r in ranks}
    if rule is not None and (tenure := tenure_of(rule, found, today)) is not None:
        out.add(f"tenure:{label(tenure)}")
    return sorted(out)


def change_text(ctx: Ctx, before: Iterable[Account] | None, after: Iterable[Account]) -> str:
    """A person's labels before and after a change, for a plan's entry. `before` is None for a person the change
    makes, who has no labels yet."""
    today = ctx.now.date()
    old = labels_of(before, ctx.config, today) if before is not None else []
    new = labels_of(after, ctx.config, today)
    return "labels unchanged" if old == new else f"labels {', '.join(old) or 'none'} → {', '.join(new)}"


async def person_of(db: Queries, guild_id: int, user_id: int) -> Person | None:
    return await db.fetchrow(
        Person,
        "SELECT id, user_id, note, pinned_main FROM people WHERE guild_id = $1 AND user_id = $2",
        guild_id,
        user_id,
    )


async def person_by_id(db: Queries, guild_id: int, person_id: int) -> Person | None:
    return await db.fetchrow(
        Person, "SELECT id, user_id, note, pinned_main FROM people WHERE guild_id = $1 AND id = $2", guild_id, person_id
    )


async def accounts_of(db: Queries, guild_id: int, person_id: int) -> list[Account]:
    return await db.fetch(
        Account,
        f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1 AND person_id = $2 ORDER BY player_id",
        guild_id,
        person_id,
    )


async def account_named(db: Queries, guild_id: int, name: str) -> Account | None:
    """The account with this name now, else the one that had it most recently."""
    found = await db.fetchrow(
        Account,
        f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1 AND key = $2 ORDER BY state = 'left', player_id LIMIT 1",
        guild_id,
        key(name),
    )
    return found


async def all_people(db: Queries, guild_id: int) -> tuple[list[Person], dict[int, list[Account]]]:
    """Every person, and their confirmed accounts by person."""
    people = await db.fetch(
        Person, "SELECT id, user_id, note, pinned_main FROM people WHERE guild_id = $1 ORDER BY id", guild_id
    )
    owned: dict[int, list[Account]] = {}
    for a in await db.fetch(
        Account, f"SELECT {ACCOUNT} FROM accounts WHERE guild_id = $1 AND person_id IS NOT NULL", guild_id
    ):
        assert a.person_id is not None
        owned.setdefault(a.person_id, []).append(a)
    return people, owned


async def ensure_person(tx: Tx, guild_id: int, user_id: int) -> int:
    """The member's person, made when they have none."""
    found = await tx.fetchval("SELECT id FROM people WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)
    if found is not None:
        return found
    made = await tx.fetchval(
        int, "INSERT INTO people (guild_id, user_id) VALUES ($1, $2) RETURNING id", guild_id, user_id
    )
    assert made is not None
    return made


async def merge(tx: Tx, guild_id: int, into: int, other: int) -> None:
    """Move every account of `other` into `into`, and delete `other`."""
    await tx.execute("UPDATE accounts SET person_id = $3 WHERE guild_id = $1 AND person_id = $2", guild_id, other, into)
    await tx.execute("DELETE FROM people WHERE guild_id = $1 AND id = $2", guild_id, other)


async def notice(tx: Tx, guild_id: int, kind: str, text: str) -> None:
    await tx.execute("INSERT INTO notices (guild_id, kind, text) VALUES ($1, $2, $3)", guild_id, kind, text)
