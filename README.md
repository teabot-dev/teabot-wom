# Wise Old Man

Keep Discord in step with your clan's [Wise Old Man](https://wiseoldman.net) group. Members link their game accounts, an admin confirms each link, and a member's accounts become one person, whose main, alts and join dates teabot follows through renames. The plugin gives every linked member labels, such as `in_clan` or `rank:sapphire`, which `admin_members` and other plugins filter on. Install it from the [plugin hub](https://teabot-dev.github.io/teabot/hub/), then turn it on in a server with `/plugins enable plugin:wom`.

## Set it up

```text
/plugins config plugin:wom setting:group_id value:651
/plugins config plugin:wom setting:admin_channel value:#admins
/wom post channel:#link-your-accounts
```

| Setting | What it is |
|---|---|
| `group_id` | The clan's Wise Old Man group, from its page's address. |
| `away_days` | Days an account may be missing from the group before it counts as left. `7` if left out. |
| `clan_role` | The Discord role clan members hold, so `wom_gaps` can list holders with no linked account. |
| `admin_channel` | Where the weekly digest, and a held sync, are posted. |
| `rank_rule` | Your clan's ranks by days in the clan, for `due:` labels and the rank audit, such as `{"count_from": "current_stint", "ranks": [{"rank": "sapphire", "days": 0}, {"rank": "emerald", "days": 365}]}`. Leave it out if your clan ranks some other way. |

Once an hour, teabot reads the group: who joined, who left, ranks, and renames. An account missing from the group counts as away for `away_days`, still in the clan, before it counts as left, so a rename that Wise Old Man has not approved yet costs nobody anything.

When more than a tenth of the clan's accounts, and more than five, go missing at once, teabot changes nothing and tells the admin channel, since a broken clan sync looks the same as a clear-out. If they really left, `/wom accept_sync` takes the group as it is.

## For members

| Command | What it does |
|---|---|
| `/wom claim names:` | Claim your accounts, your main and its alts, separated by commas. An admin confirms each one. The link post's **Link accounts** button does the same. |
| `/wom drop name:` | Drop one of your accounts, or a claim still waiting. |
| `/wom me` | Your accounts, your main and your join dates, with a link to Wise Old Man's form for a rename it missed. |

## For admins

These need Manage Server, and also work for AI assistants and the service API.

| Command | What it does |
|---|---|
| `/wom claims` | Claims waiting for you, with what supports each: whether the account is in the group, its rank, and which of the member's Discord names match it. Also suggests unlinked accounts a member's name matches. |
| `/wom confirm` and `/wom reject` | Settle claims by their numbers, up to ten at once. Confirming shows a plan of each member's labels before and after. |
| `/wom link` and `/wom unlink` | Put an account in a person by hand, a member or someone not on Discord, with their first join date if you know it, or take it out. |
| `/wom merge` and `/wom split` | Merge someone not on Discord into a member, or move accounts out into a new person. |
| `/wom forget` | Forget a member linked by mistake. |
| `/wom pin_main` | Choose a person's main, in place of the account that joined first. |
| `/wom set_joined` | Set an account's join dates by hand. |
| `/wom people` | Everyone, their accounts, main and join date. |
| `/wom gaps` | What needs you: unlinked clan accounts, competing claims, merges, archives, missing join dates, banned accounts, and the rank audit. |

Changes to who holds which account are destructive, since other plugins act on labels: they show a plan first, and an AI assistant needs a token made with `max_risk:destructive`.

## Labels

Every member with a confirmed account gets:

- `linked`, which stays after they drop every account;
- `in_clan` while one of their accounts is in the clan or away, and `left` once none is;
- `rank:<rank>` for each in-game rank one of their accounts in the clan holds;
- with a `rank_rule`, `due:<rank>` for the rank the rule gives for their days in the clan, whatever ranks their accounts hold in game, while one of them is in the clan.

They count for one day after the last good sync, so a Wise Old Man outage of more than a day leaves them matching nobody.

## The rank audit

A person's days in the clan come from Wise Old Man's join dates, which RuneLite's clan sync reads from the game, so a role rule on `due:` labels ranks Discord by time in the clan. Wise Old Man can't change in-game ranks, so with a `rank_rule` set, `/wom gaps` lists each account whose rank differs from what the rule gives for its person's days in the clan, counted from their first join ever or their current stint. Ranks the rule doesn't name, such as staff ranks, are left alone. Fix them in game.

## Instance settings

| Variable | What it is |
|---|---|
| `TEABOT_PLUGIN_WOM_API_KEY` | Optional. A key from Wise Old Man's Discord, which raises its limits from 20 requests a minute to 100. An instance with many servers needs one. |

## External connections

- `api.wiseoldman.net`: the group's memberships and name changes, hourly, and a player by name when an admin links an account outside the group. Requests name this plugin's repository in their User-Agent, and nothing about the instance.

## Data

The plugin keeps each server's accounts from its group, people, members' claims, and notices of merges and archives. Account names are public, through Wise Old Man and the hiscores; the link to a Discord member is what it adds. `teabot users delete` removes a member's people and claims. Unlinked accounts that left are deleted a year after they left.
