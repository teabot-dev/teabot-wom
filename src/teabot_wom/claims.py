"""What members do: claim their accounts, drop one, and see their own; and the link channel's post.

Every claim waits for an admin, since a wrong link would hand someone another person's history. A waiting claim
feeds nothing: no main, dates or labels, until it is confirmed.
"""

from datetime import timedelta
from typing import Annotated

from pydantic import Field
from teabot.sdk import ActionError, Button, TextChannel

from teabot_wom import Ctx, plugin
from teabot_wom.names import NAME_MAX, key, names_in
from teabot_wom.people import accounts_of, main_of, person_of
from teabot_wom.sync import relabel
from teabot_wom.wom import NAME_CHANGE_FORM

CLAIMS_AT_ONCE = 5
CLAIMS_A_DAY = 10


@plugin.action()
async def claim(ctx: Ctx, names: Annotated[str, Field(max_length=100)]) -> str:
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
    recent = await ctx.db.fetchval(
        int,
        "SELECT count(*) FROM claims WHERE guild_id = $1 AND user_id = $2 AND created_at > $3",
        ctx.guild.id,
        me,
        ctx.now - timedelta(days=1),
    )
    if recent + len(wanted) > CLAIMS_A_DAY:
        raise ActionError(f"You can make {CLAIMS_A_DAY} claims a day. Try again tomorrow.")
    person = await person_of(ctx.db, ctx.guild.id, me)
    owned = {a.key for a in await accounts_of(ctx.db, ctx.guild.id, person.id)} if person else set[str]()
    made: list[str] = []
    async with ctx.db.transaction() as tx:
        for name in wanted:
            k = key(name)
            waiting = await tx.fetchval(
                int,
                "SELECT count(*) FROM claims WHERE guild_id = $1 AND user_id = $2 AND key = $3 AND state = 'waiting'",
                ctx.guild.id,
                me,
                k,
            )
            if k in owned or waiting:
                continue
            await tx.execute(
                "INSERT INTO claims (guild_id, user_id, name, key) VALUES ($1, $2, $3, $4)", ctx.guild.id, me, name, k
            )
            made.append(name)
    if not made:
        return "Those are already yours, or already waiting for an admin."
    return f"Claimed {', '.join(made)}. An admin confirms each claim, usually within a day."


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
        for a in accounts:
            star = " (main)" if main is not None and a.player_id == main.player_id else ""
            since = f", joined {a.joined}" if a.joined else ""
            lines.append(f"- **{a.name}**{star}: {a.rank or 'no rank'}, {a.state}{since}")
    waiting = [
        str(r["name"])
        for r in await ctx.db.fetch(
            "SELECT name FROM claims WHERE guild_id = $1 AND user_id = $2 AND state = 'waiting' ORDER BY id",
            ctx.guild.id,
            ctx.actor.user_id,
        )
    ]
    lines += [f"- {n}: waiting for an admin" for n in waiting]
    if not lines:
        return "You have no accounts linked yet. Claim them with `/wom claim`."
    lines.append(f"Renamed and teabot missed it? Tell Wise Old Man: {NAME_CHANGE_FORM}")
    return "\n".join(lines)


@plugin.action(needs="manage_guild")
async def post(ctx: Ctx, channel: TextChannel) -> str:
    """Post the link channel's message, with a button members press to claim their accounts.

    Args:
        channel: Where to post it
    """
    await channel.send(
        "**Link your game accounts**\n"
        "Press **Link accounts** and type every account you play here, your main and its alts. An admin confirms "
        "each one, usually within a day, so admins can see who is who in the clan.",
        buttons=[
            Button(claim.asking(), label="Link accounts", style="primary"),
            Button(me.bind(), label="My accounts"),
        ],
    )
    return f"Posted in <#{channel.id}>."
