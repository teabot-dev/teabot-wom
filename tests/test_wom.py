"""The wom plugin against a fake Wise Old Man: syncing the group, claims and confirmations, the labels they give,
renames and merges, the guard on many accounts missing at once, and the rank audit."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import discord
import pytest
from teabot.testing import FakeButton, FakeChannel, HarnessFactory, Planned, PluginHarness

from teabot_wom import RankRule, RankStep, WomConfig, admin, plugin, report, wom
from teabot_wom.claims import claim, me, post
from teabot_wom.sync import sync

GROUP = 651
BASE = "https://api.wiseoldman.net/v2"
NONE = discord.Permissions.none()


@pytest.fixture(autouse=True)
def no_pacing() -> Iterator[None]:
    wom.pacer.gap = 0
    yield
    wom.pacer.gap = None


def membership(player_id: int, name: str, rank: str = "sapphire", joined: str = "2024-01-01") -> dict[str, Any]:
    return {
        "playerId": player_id,
        "role": rank,
        "clientSyncJoinedAt": f"{joined}T00:00:00.000Z",
        "player": {"id": player_id, "displayName": name, "type": "regular", "status": "active"},
    }


def serve(harness: PluginHarness, members: list[dict[str, Any]], changes: list[dict[str, Any]] | None = None) -> None:
    harness.web.reply(
        f"{BASE}/groups/{GROUP}", json_body={"name": "Tea", "updatedAt": "2026-11-17T16:40:53Z", "memberships": members}
    )
    harness.web.reply(f"{BASE}/groups/{GROUP}/name-changes?limit=50&offset=0", json_body=changes or [])


async def setup(teabot_harness: HarnessFactory, **config: Any) -> PluginHarness:
    harness = await teabot_harness(plugin)
    await harness.configure(WomConfig(group_id=GROUP, **config))
    db: Any = harness.admin
    await db.execute("INSERT INTO core.directory_state (guild_id, backfilled) VALUES ($1, true)", harness.guild.id)
    return harness


async def run_sync(harness: PluginHarness) -> None:
    await harness.run_job(sync.bind())
    await harness.advance(minutes=2)
    await harness.run_jobs()


async def settle(harness: PluginHarness, plan: Planned) -> Planned:
    """Confirm a plan, and let the labels it changes be written."""
    await harness.confirm(plan)
    await harness.advance(minutes=2)
    await harness.run_jobs()
    return plan


async def confirm_all(harness: PluginHarness) -> Planned:
    page = await harness.call(report.claims)
    return await settle(harness, await harness.plan(admin.confirm, claims=[c.id for c in page.claims]))


async def labels(harness: PluginHarness, user_id: int) -> list[str]:
    db: Any = harness.admin
    found = await db.fetchval(
        "SELECT labels FROM core.member_labels WHERE guild_id = $1 AND plugin_id = 'wom' AND user_id = $2",
        harness.guild.id,
        user_id,
    )
    return list(found or [])


async def account(harness: PluginHarness, player_id: int) -> Any:
    db: Any = harness.admin
    return await db.fetchrow("SELECT * FROM plugin_wom.accounts WHERE player_id = $1", player_id)


async def test_a_claim_waits_for_an_admin_then_gives_labels(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    fred = harness.guild.add_member(NONE, name="fred")
    serve(harness, [membership(1, "Fred Main", "silver_key"), membership(2, "Fred Iron", "blue_gem")])
    await run_sync(harness)
    reply = await harness.as_member(fred).call(claim, names="Fred Main, fred_iron")
    assert "Claimed `Fred Main`, `Fred Iron`" in reply
    assert await labels(harness, fred.id) == []
    plan = await confirm_all(harness)
    assert plan.counts == {"confirm": 2}
    assert await labels(harness, fred.id) == ["in_clan", "linked", "rank:blue_gem", "rank:silver_key"]


async def test_an_account_missing_is_away_then_left(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness, away_days=7)
    fred = harness.guild.add_member(NONE, name="fred")
    others = [membership(10 + i, f"Other {i}") for i in range(10)]
    serve(harness, [membership(1, "Fred"), *others])
    await run_sync(harness)
    await harness.as_member(fred).call(claim, names="Fred")
    await confirm_all(harness)
    serve(harness, others)
    await run_sync(harness)
    assert (await account(harness, 1))["state"] == "away"
    assert "in_clan" in await labels(harness, fred.id)
    await harness.advance(days=8)
    await run_sync(harness)
    assert (await account(harness, 1))["state"] == "left"
    assert await labels(harness, fred.id) == ["left", "linked"]


async def test_many_accounts_missing_at_once_hold_the_sync(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    everyone = [membership(i, f"Member {i}") for i in range(1, 21)]
    serve(harness, everyone)
    await run_sync(harness)
    serve(harness, everyone[:10])
    await run_sync(harness)
    assert (await account(harness, 1))["state"] == "in"
    await settle(harness, await harness.plan(admin.accept_sync))
    await harness.run_jobs()
    assert (await account(harness, 15))["state"] == "away"


async def test_a_rename_keeps_the_account_and_its_dates(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    serve(harness, [membership(1, "Old Name", joined="2023-05-01")])
    await run_sync(harness)
    serve(harness, [membership(1, "New Name", joined="2023-05-01")])
    await run_sync(harness)
    row = await account(harness, 1)
    assert (row["name"], list(row["past_names"]), row["joined"]) == ("New Name", ["Old Name"], date(2023, 5, 1))


async def test_a_merge_moves_the_vanished_accounts_person_onto_the_survivor(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    fred = harness.guild.add_member(NONE, name="fred")
    serve(harness, [membership(1, "Fred", joined="2021-01-01"), membership(2, "Zed")])
    await run_sync(harness)
    await harness.as_member(fred).call(claim, names="Fred")
    await confirm_all(harness)
    change = {
        "id": 77,
        "playerId": 2,
        "oldName": "Zed",
        "newName": "Fred",
        "status": "approved",
        "createdAt": "2026-11-18T10:00:00Z",
    }
    serve(harness, [membership(2, "Fred", joined="2024-06-01")], [change])
    await run_sync(harness)
    assert await account(harness, 1) is None
    survivor = await account(harness, 2)
    assert survivor["first_joined"] == date(2021, 1, 1)
    assert survivor["person_id"] is not None


async def test_a_rejoin_after_leaving_starts_a_new_stint(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness, away_days=1)
    others = [membership(10 + i, f"Other {i}") for i in range(10)]
    serve(harness, [membership(1, "Fred", joined="2020-01-01"), *others])
    await run_sync(harness)
    serve(harness, others)
    await run_sync(harness)
    await harness.advance(days=2)
    await run_sync(harness)
    serve(harness, [membership(1, "Fred", joined="2026-11-01"), *others])
    await run_sync(harness)
    row = await account(harness, 1)
    assert (row["joined"], row["first_joined"]) == (date(2026, 11, 1), date(2020, 1, 1))


async def test_the_rank_audit_lists_ranks_the_rule_calls_for(teabot_harness: HarnessFactory) -> None:
    rule = RankRule(ranks=[RankStep(rank="sapphire", days=0), RankStep(rank="emerald", days=365)])
    harness = await setup(teabot_harness, rank_rule=rule)
    fred = harness.guild.add_member(NONE, name="fred")
    joined = "2020-01-01"
    serve(harness, [membership(1, "Fred", "sapphire", joined), membership(2, "Staff", "silver_key", joined)])
    await run_sync(harness)
    await harness.as_member(fred).call(claim, names="Fred")
    await confirm_all(harness)
    gaps = {g.kind: g.examples for g in (await harness.call(report.gaps)).gaps}
    assert gaps["in-game ranks to promote"] == ["Fred: sapphire → emerald"]
    assert gaps["clan accounts with no member"] == ["Staff"]


async def test_tenure_is_the_rank_days_give_whatever_rank_is_held(teabot_harness: HarnessFactory) -> None:
    rule = RankRule(ranks=[RankStep(rank="sapphire", days=0), RankStep(rank="emerald", days=365)])
    harness = await setup(teabot_harness, rank_rule=rule)
    fred = harness.guild.add_member(NONE, name="fred")
    serve(harness, [membership(1, "Fred", "silver_key", "2020-01-01"), membership(2, "Fred Iron", "sapphire")])
    await run_sync(harness)
    await harness.as_member(fred).call(claim, names="Fred")
    await confirm_all(harness)
    assert await labels(harness, fred.id) == ["in_clan", "linked", "rank:silver_key", "tenure:emerald"]


async def test_a_member_ranked_up_early_keeps_the_rank(teabot_harness: HarnessFactory) -> None:
    rule = RankRule(ranks=[RankStep(rank="sapphire", days=0), RankStep(rank="emerald", days=365)])
    harness = await setup(teabot_harness, rank_rule=rule)
    fred = harness.guild.add_member(NONE, name="fred")
    serve(harness, [membership(1, "Fred", "emerald", "2026-11-01")])
    await run_sync(harness)
    await harness.as_member(fred).call(claim, names="Fred")
    await confirm_all(harness)
    assert "tenure:emerald" in await labels(harness, fred.id)
    kinds = {g.kind for g in (await harness.call(report.gaps)).gaps}
    assert "in-game ranks to promote" not in kinds


async def test_the_link_post_asks_for_names_in_a_form(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    channel = harness.guild.add_channel(FakeChannel(name="link-your-accounts"))
    serve(harness, [membership(1, "Fred")])
    await run_sync(harness)
    await harness.call(post, channel=channel)
    (sent,) = channel.sent
    button: FakeButton = sent.buttons[0]
    reply = await harness.press(button, typed={"names": "Fred"})
    assert reply.startswith("Claimed `Fred`")


async def test_a_suggestion_says_which_name_matched(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    member = harness.guild.add_member(NONE, name="slow pepsi")
    db: Any = harness.admin
    await db.execute(
        "INSERT INTO core.members (guild_id, user_id, name, observed_at) VALUES ($1, $2, $3, now())",
        harness.guild.id,
        member.id,
        "slow pepsi",
    )
    serve(harness, [membership(1, "Slow Pepsi")])
    await run_sync(harness)
    serve(harness, [membership(1, "PEPS1")])
    await run_sync(harness)
    (found,) = (await harness.call(report.claims)).suggestions
    assert (str(found.name), str(found.matched), found.past_name) == ("PEPS1", "Slow Pepsi", True)


async def test_gaps_say_when_no_sync_has_run(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    assert (await harness.call(report.gaps)).last_sync is None
    serve(harness, [membership(1, "Fred")])
    await run_sync(harness)
    assert (await harness.call(report.gaps)).last_sync is not None


async def test_my_accounts_show_which_are_linked_and_which_wait(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    fred = harness.guild.add_member(NONE, name="fred")
    serve(harness, [membership(1, "Fred Main", joined="2024-01-01"), membership(2, "Fred Iron")])
    await run_sync(harness)
    await harness.as_member(fred).call(claim, names="Fred Main")
    await confirm_all(harness)
    await harness.as_member(fred).call(claim, names="fred iron")
    shown = await harness.as_member(fred).call(me)
    assert "🟢 `Fred Main` · main · sapphire · in the clan · since 1 January 2024" in shown
    assert "🟡 `Fred Iron` · waiting for an admin" in shown


async def test_a_name_outside_the_group_is_turned_away_with_close_names(teabot_harness: HarnessFactory) -> None:
    harness = await setup(teabot_harness)
    fred = harness.guild.add_member(NONE, name="fred")
    serve(harness, [membership(1, "Green Clog"), membership(2, "Fred")])
    await run_sync(harness)
    reply = await harness.as_member(fred).call(claim, names="gren clog\nfred\ndsfgsdfg")
    assert "Claimed `Fred`" in reply
    assert "`gren clog` isn't in the clan's Wise Old Man group. Did you mean `Green Clog`?" in reply
    assert "`dsfgsdfg` isn't in the clan's Wise Old Man group. If you joined or renamed recently" in reply
    page = await harness.call(report.claims)
    assert [str(c.name) for c in page.claims] == ["Fred"]


async def test_an_admin_links_a_main_and_its_alts_in_one_plan(teabot_harness: HarnessFactory) -> None:
    rule = RankRule(ranks=[RankStep(rank="sapphire", days=0), RankStep(rank="emerald", days=365)])
    harness = await setup(teabot_harness, rank_rule=rule, inactive_ranks=["burnt"])
    kala = harness.guild.add_member(NONE, name="kala")
    bob = harness.guild.add_member(NONE, name="bob")
    serve(
        harness,
        [
            membership(1, "OG Kala", "sapphire", "2020-01-01"),
            membership(2, "Drunk Kala", "sapphire", "2026-10-01"),
            membership(3, "OF Kala", "burnt", "2023-01-01"),
            membership(4, "Old Bob", "burnt", "2026-01-01"),
        ],
    )
    await run_sync(harness)
    plan = await harness.plan(admin.link, names=["OG Kala", "drunk kala", "OF Kala"], member=kala)
    assert plan.counts == {"link": 1}
    await settle(harness, plan)
    # Kala plays an active account, so OF Kala's inactive rank gives no label; Bob's only account is inactive.
    assert await labels(harness, kala.id) == ["in_clan", "linked", "rank:sapphire", "tenure:emerald"]
    await settle(harness, await harness.plan(admin.link, names=["Old Bob"], member=bob))
    assert await labels(harness, bob.id) == ["in_clan", "linked", "rank:burnt", "tenure:sapphire"]
    gaps = {g.kind: g.examples for g in (await harness.call(report.gaps)).gaps}
    assert gaps["in-game ranks to promote"] == ["Drunk Kala: sapphire → emerald", "OG Kala: sapphire → emerald"]
