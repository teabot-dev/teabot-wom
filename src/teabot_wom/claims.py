"""What members do: claim their accounts, drop one, and see their own; and the link channel's post.

A claim names an account in the clan's group; a name that isn't is turned away, with the group's closest names.
Every claim waits for an admin, since a wrong link would hand someone another person's history. A waiting claim
feeds nothing: no main, dates or labels, until it is confirmed.
"""

import difflib
from datetime import timedelta
from typing import Annotated

from pydantic import Field
from teabot.sdk import ActionError, Button, LongText, TextChannel

from teabot_wom import Ctx, plugin
from teabot_wom.names import NAME_MAX, key, names_in
from teabot_wom.people import Account, accounts_of, main_of, person_of
from teabot_wom.sync import relabel
from teabot_wom.wom import NAME_CHANGE_FORM

CLAIMS_AT_ONCE = 5
CLAIMS_A_DAY = 10
# Clan accounts suggested for a name that isn't in the group, and how close they must be.
CLOSE_MATCHES = 3
CLOSE_CUTOFF = 0.75
# Discord has no coloured text, so `/wom me` marks each account's status with a coloured circle.
WAITING = "🟡"
LINKED = "🟢"
STATES = {
    "in": "in the clan",
    "away": "missing from the group for now",
    "left": "left the clan",
    "outside": "not in the clan",
}


@plugin.action()
async def claim(ctx: Ctx, names: LongText) -> str:
    """Claim your Wise Old Man accounts: your main and its alts. An admin confirms each claim.

    Args:
        names: Your account names, one per line or separated by commas
    """
    wanted = names_in(names)
    if not wanted:
        raise ActionError("Give at least one account name.")
    if len(wanted) > CLAIMS_AT_ONCE:
        raise ActionError(f"Claim at most {CLAIMS_AT_ONCE} accounts at once.")
    if bad := [n for n in wanted if len(n) > NAME_MAX]:
        raise ActionError(f"{bad[0]} is longer than an account name can be.")
    me = ctx.actor.user_id
    in_group = {
        str(r["key"]): str(r["name"])
        for r in await ctx.db.fetch(
            "SELECT key, name FROM accounts WHERE guild_id = $1 AND state IN ('in', 'away')", ctx.guild.id
        )
    }
    person = await person_of(ctx.db, ctx.guild.id, me)
    owned = {a.key for a in await accounts_of(ctx.db, ctx.guild.id, person.id)} if person else set[str]()
    waiting = {
        str(r["key"])
        for r in await ctx.db.fetch(
            "SELECT key FROM claims WHERE guild_id = $1 AND user_id = $2 AND state = 'waiting'", ctx.guild.id, me
        )
    }
    new: list[tuple[str, str]] = []
    already: list[str] = []
    unknown: list[str] = []
    for name in wanted:
        k = key(name)
        if k not in in_group:
            unknown.append(name)
        elif k in owned or k in waiting:
            already.append(in_group[k])
        elif k not in {n for n, _ in new}:
            # An account in the group is claimed by its own name, whatever the member's spelling.
            new.append((k, in_group[k]))
    recent = await ctx.db.fetchval(
        int,
        "SELECT count(*) FROM claims WHERE guild_id = $1 AND user_id = $2 AND created_at > $3",
        ctx.guild.id,
        me,
        ctx.now - timedelta(days=1),
    )
    if recent + len(new) > CLAIMS_A_DAY:
        raise ActionError(f"You can make {CLAIMS_A_DAY} claims a day. Try again tomorrow.")
    async with ctx.db.transaction() as tx:
        for k, name in new:
            await tx.execute(
                "INSERT INTO claims (guild_id, user_id, name, key) VALUES ($1, $2, $3, $4)", ctx.guild.id, me, name, k
            )
    lines: list[str] = []
    if new:
        listed = ", ".join(f"`{_plain(n)}`" for _, n in new)
        lines.append(f"Claimed {listed}. An admin confirms each claim, usually within a day. See them with `/wom me`.")
    if already:
        lines.append(f"Already yours or waiting: {', '.join(f'`{_plain(n)}`' for n in already)}.")
    lines += [_not_in_group(name, in_group) for name in unknown]
    return "\n".join(lines)


def _not_in_group(name: str, in_group: dict[str, str]) -> str:
    """Why a name was turned away, with the clan's accounts it is closest to."""
    close = difflib.get_close_matches(key(name), list(in_group), n=CLOSE_MATCHES, cutoff=CLOSE_CUTOFF)
    said = f"`{_plain(name)}` isn't in the clan's Wise Old Man group."
    if close:
        return f"{said} Did you mean {' or '.join(f'`{_plain(in_group[k])}`' for k in close)}?"
    return f"{said} If you joined or renamed recently, try again once Wise Old Man has caught up, usually within a day."


@plugin.action()
async def drop(ctx: Ctx, name: Annotated[str, Field(max_length=NAME_MAX)]) -> str:
    """Drop one of your accounts, or a claim waiting for an admin.

    Args:
        name: The account's name
    """
    me, k = ctx.actor.user_id, key(name)
    async with ctx.db.transaction() as tx:
        cleared = await tx.execute(
            "DELETE FROM claims WHERE guild_id = $1 AND user_id = $2 AND key = $3 AND state = 'waiting'",
            ctx.guild.id,
            me,
            k,
        )
        unlinked = await tx.execute(
            "UPDATE accounts SET person_id = NULL WHERE guild_id = $1 AND key = $3 AND person_id = "
            "(SELECT id FROM people WHERE guild_id = $1 AND user_id = $2)",
            ctx.guild.id,
            me,
            k,
        )
        if unlinked != "UPDATE 0":
            await relabel(tx, ctx)
    if cleared == "DELETE 0" and unlinked == "UPDATE 0":
        return f"{name} is neither yours nor waiting."
    return f"Dropped {name}."


@plugin.action()
async def me(ctx: Ctx) -> str:
    """Your Wise Old Man accounts, your main and your join dates, as teabot sees them."""
    lines: list[str] = []
    person = await person_of(ctx.db, ctx.guild.id, ctx.actor.user_id)
    if person is not None:
        accounts = await accounts_of(ctx.db, ctx.guild.id, person.id)
        main = main_of(person, accounts)
        lines += [_line(a, main is not None and a.player_id == main.player_id) for a in accounts]
    waiting = [
        str(r["name"])
        for r in await ctx.db.fetch(
            "SELECT coalesce(a.name, c.name) AS name FROM claims c LEFT JOIN accounts a "
            "ON a.guild_id = c.guild_id AND a.key = c.key "
            "WHERE c.guild_id = $1 AND c.user_id = $2 AND c.state = 'waiting' ORDER BY c.id",
            ctx.guild.id,
            ctx.actor.user_id,
        )
    ]
    lines += [f"{WAITING} `{_plain(n)}` · waiting for an admin" for n in waiting]
    if not lines:
        return "You have no accounts linked yet. Claim them with `/wom claim`."
    return "\n".join(
        [
            "**Your accounts**",
            *lines,
            "",
            f"Renamed and teabot missed it? Tell Wise Old Man: <{NAME_CHANGE_FORM}>",
        ]
    )


def _plain(name: str) -> str:
    """A name safe inside backticks."""
    return name.replace("`", "")


def _line(a: Account, main: bool) -> str:
    parts = [
        f"{LINKED} `{_plain(a.name)}`",
        *(["main"] if main else []),
        a.rank or "no rank",
        STATES.get(a.state, a.state),
    ]
    if a.joined and a.present:
        parts.append(f"since {a.joined:%-d %B %Y}")
    return " · ".join(parts)


@plugin.action(needs="manage_guild")
async def post(ctx: Ctx, channel: TextChannel) -> str:
    """Post the link channel's message, with a button members press to claim their accounts.

    Args:
        channel: Where to post it
    """
    await channel.send(
        "**Link your game accounts**\n"
        "Press **Link accounts** and type every account you play, your main and its alts, one per line. An admin confirms "
        "each one, usually within a day, so admins can see who is who in the clan.",
        buttons=[
            Button(claim.asking(), label="Link accounts", style="primary"),
            Button(me.bind(), label="My accounts"),
        ],
    )
    return f"Posted in <#{channel.id}>."
