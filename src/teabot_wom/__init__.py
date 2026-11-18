"""teabot-wom: a server's Wise Old Man group, its members' game accounts, and labels that role rules match. Imports
only `teabot.sdk`.

- `sync`, hourly, reads the group: joins, leaves, ranks and renames. An account missing from the group is away for a
  grace period before it counts as left, so a rename's in-between state costs nobody their role. A sync in which many
  accounts go missing at once is held for an admin, since a broken clan sync looks the same.
- Members claim their accounts with `wom.claim`, or the link channel's button, and an admin confirms each claim.
  A member's confirmed accounts are one person, whose labels the plugin writes: `linked`, `in_clan` or `left`, and
  `rank:<rank>` for each in-game rank they hold.
- `wom.gaps` lists what needs a person, with the rank audit for a server that sets a rank rule, and `digest` posts
  it weekly to the admin channel.
"""

from typing import Literal

from pydantic import BaseModel, Field
from teabot.sdk import ChannelRef, Context, Plugin, RoleRef

from teabot_wom.wom import Keys


class RankStep(BaseModel):
    rank: str = Field(description="An in-game rank, as WOM names it, such as sapphire")
    days: int = Field(ge=0, description="Days in the clan it takes")


class RankRule(BaseModel):
    """The clan's own rule for in-game ranks, by time in the clan, which the rank audit checks the game against."""

    count_from: Literal["first_joined", "current_stint"] = "current_stint"
    ranks: list[RankStep] = []


class WomConfig(BaseModel):
    group_id: int | None = Field(default=None, description="The clan's Wise Old Man group")
    away_days: int = Field(default=7, ge=1, le=60, description="Days an account may be missing before it has left")
    clan_role: RoleRef | None = Field(default=None, description="The Discord role clan members hold, for wom.gaps")
    admin_channel: ChannelRef | None = Field(default=None, description="Where the weekly digest goes")
    rank_rule: RankRule | None = Field(default=None, description="Ranks by days in the clan, for the rank audit")


plugin = Plugin(
    "wom",
    config=WomConfig,
    hosts=["api.wiseoldman.net"],
    reads=["members"],
    writes=["labels"],
    description="Members link their Wise Old Man accounts, which group into people and give labels role rules match.",
)
type Ctx = Context[WomConfig]
keys = plugin.instance_settings(Keys)

from teabot_wom import admin as admin
from teabot_wom import claims as claims
from teabot_wom import report as report
from teabot_wom import sync as sync
