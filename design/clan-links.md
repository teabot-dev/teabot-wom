# Clan links: a Wise Old Man plugin, and how Tea uses it

The design, reviewed until it converged: Discord members link their Wise Old Man accounts, a member's accounts group into one person, names follow renames, and roles follow what a plugin says about members, through rules in the roles plugin. Member labels and buttons that ask are in teabot 1.12, and this repository holds the plugin; role rules come next. Keeping nicknames matching accounts is a server's own choice, so the plugin doesn't do it.

## The problem

- Tea's ranks follow time in the in-game clan. The clan lives in the game, and Discord only mirrors it.
- Wise Old Man (WOM) tracks the clan as a group, from the clan sync of RuneLite's WOM plugin. Each membership gives the account's in-game rank (`role`) and, when clan sync has seen it, the day it joined the in-game clan (`clientSyncJoinedAt`, a date at midnight UTC). 
- A person can have several accounts. Which accounts belong together is known only by hand.
- Which Discord member owns which account is known only by hand too.
- Players rename their accounts. WOM follows most renames, and the upkeep beyond that is by hand.

## What is generic, and what is Tea's

- **Generic:** the SDK additions, role rules, and the wom plugin. Linking Discord members to WOM accounts, grouping accounts into people, following renames, mirroring in-game ranks, and filtering members by what a plugin says about them suit any clan with a WOM group.
- **Tea's own choice:** ranking people by time in the clan, with a person's oldest account setting the rank of their alts. Another clan may rank by level, by role in events, or not at all. The plugin offers the data and assumes no rule. Tea's rule is one setting, `rank_rule`, which the rank audit checks the game against.

## Part 1: SDK and core

### Spike S11: WOM's data, as it really behaves

Run on 2026-11-18 against Tea's group ([spikes.md](spikes.md#s11-wise-old-mans-data-for-clan-links)). Every membership has `clientSyncJoinedAt`, so the rank audit goes ahead. A rename keeps the account's ID and join date. WOM's record holds a broken sync that dropped three quarters of the group for a day.

### Member labels in the directory

- A plugin declaring `Plugin(..., writes=["labels"])` keeps, per member, a set of **labels** under its own plugin ID, such as `wom`: short strings it chooses, such as `in_clan`, `left` or `rank:sapphire`. The core gives them no meaning, so it holds no concept of a rank, an account or a level.
- It writes them with `ctx.labels.replace_all(labels_by_member, as_of=)`, which replaces the server's whole set for that plugin in one transaction and records the server's state for it: `as_of`, how fresh the plugin's data is, such as its last successful fetch. The core compares inside the transaction and writes only changed rows, so a large server's hourly replace stays cheap. Labels are keyed by `teabot.member_id`, with no directory row needed, so a member who joins Discord matches at once; filters only ever match present members. It raises `DirectoryUnavailable` while the server's directory isn't ready (D55). A dropped member disappears with the next replace, so there is no separate clear, and no sync leaves some members fresh and others stale.
- The labels are a copy. The plugin's tables are the source of truth.
- **`MemberFilter` gains** `labels_from` (a plugin), `labels_any` and `labels_all`. The last two need `labels_from`, and `labels_from` alone means "has any label from it". A plugin's labels count only while it is enabled in the server and its `as_of` is within its freshness window, declared as `Plugin(..., labels_fresh_for=)`, one day by default and at most a week; otherwise members match as if they had none. Labels hold no names, so a rename never moves a member into or out of a change.
- **Labels changed, as an event.** `on_labels_changed(plugin)` fires in the server when a `replace_all` changes anyone's labels, and when a plugin's labels become fresh again or the plugin is turned back on, since scopes then fill again.
- **Several filters at once.** `ctx.members.count` and `find` take a list of filters, all of which a member must match, so a rule can ask for a step's filter within its scope. One filter names one plugin's labels; a list can combine several.
- **Discord's Linked Roles were considered.** Their metadata is per user and application, five fields at most, the same in every server, and each member must authorise it, so they can't carry per-server labels on a shared bot.
- **Limits:** at most 32 labels per member and plugin, each at most 64 characters.
- **Who sees them:** any plugin with `reads=["members"]` can filter on another plugin's labels, so labels say nothing a server's plugins shouldn't know, and the plugin guide says so.
- **Privacy and deletion:** member deletion covers labels, the site's privacy policy lists them as directory data, and the plugin's hub page shows `writes=["labels"]`.

### Buttons that ask

- `action.bind(...)` may leave text inputs out. Pressing the button opens a modal for them, through the modal path `LongText` already has. Discord's modal holds at most five inputs, so an action with more open inputs is refused at registration.
- The link channel's post gets a "Link accounts" button, so no member has to learn a slash command.

## Part 2: role rules, in the roles plugin

Nothing here is about WOM. A rule says "within scope S, each member holds the first role in an ordered list of steps whose filter they match, and none of the list's other roles". Each step is a role and a filter, over any plugin's labels or anything else a filter can express.

- **One kind, two uses.**
  - A list of one step is a plain rule, such as Clan Member following `labels_any: [in_clan]`.
  - A ladder of rank roles is one rule with a step per rank, highest first, each `labels_any: [rank:<name>]`. A member whose labels match no step holds none of the list's roles.
- **A rule is a reconciler.** It states the end result, so it stores no changes. Each run works out the difference: for each member in S, the role of their first matching step, if they lack it, and every other role of the list they hold. It applies the difference member by member, giving and taking together in each member's turn, and records one outcome per run. A holder outside S keeps their roles, so a rule over a plugin's labels never touches members the plugin knows nothing about. Members in S are found with the scope's filter, and each step's members with one query of the step's filter within S; the rule then gives each member the first step they appear in. `MemberFilter` never needs negation.
- **One pace per server.** Rule runs and the `change` job draw from one budget per server inside the roles plugin, a next-allowed time at S10's rate less headroom for pickers. A rule run takes its slots first, so a three-hour admin change never holds up a promotion. A run that can't finish within its slots queues itself for the next ones and works out the difference again.
- **When it runs:** on `on_labels_changed` for a plugin a rule names, on `on_member_roles` for a role its scope names, such as Exempt, on `on_member_join`, and once a day as a safety run. Triggers are throttled: one schedules the server's rule job a minute out only when none is pending, so a run follows the first trigger within a minute and a busy role can't keep pushing it back. Changes to the ruled roles themselves, by hand or by another bot, wait for the daily run, so a rule never fights another bot change by change. A rule's own changes trigger only the rules whose scope names one of its roles, such as a rank rule scoped to Clan Member, which then run at once.
- **Caps.** A run applies while it changes at most its cap of members: `min(25, max(3, holders / 10))` by default, where holders counts every member holding one of the rule's roles. `cap=` on the rule replaces the formula with a fixed number.
  - **Holds clear themselves.** A run above its cap does nothing and posts its counts to the log channel once, recording that it did. A later run within the cap applies, so a glitch that reverses needs no one. To let a held run through, an admin runs `rule_add` again over the same rule with `cap_once=`, whose plan is the run worked out afresh.
- **A confirmed run is uncapped.** The run `rule_add` plans, first or with `cap_once=`, was approved by its plan, and runs within the matching-change limit of 10,000. Its apply refuses with a fresh plan when the run has grown by more than a tenth, as matching changes do.
- **The log.** Rule runs post to the log channel only when held or stopped, and in a daily summary of what rules changed.
- **`roles.rule_add(steps, scope, cap=)`:** destructive. Its plan shows what the rule would change now, and when the token confirming it expires, if sooner than a month. Confirming stores the rule, with the apply's audit row as its grant, and runs it. It refuses:
  - a role that already belongs to another rule. Rules have IDs: `rule_add(rule=)` replaces that rule in place, such as to add a step, with a plan of what the new version changes, and renews its grant, which is how a rule outlives a confirmer who left;
  - a step naming a plugin's labels when the scope has no positive condition on that plugin's labels (`labels_any` or `labels_all`), since stale labels would then take roles from everyone; with one, stale labels empty the scope and nothing is taken;
  - a role with a dangerous permission, on every door, since rules act on outside data with no member confirming each run.
- **Exemptions** need nothing new: a scope with `roles_none: [Exempt]` leaves out members holding Exempt. The docs say so.
- **Where they're made:** on MCP, the service API and the site, like matching changes. The docs recommend the site, since a rule made through a token stops when the token expires.
- **The grant can end.** A rule stops when its confirmer loses the rights or their token expires, with the reason and the renew path in the log channel. A rule made on the site has no token.
- **A rule owns its roles.** `roles.give`, `roles.take` and matching changes on a ruled role warn in their plan that the rule will undo them. Pickers refuse a ruled role, and `rule_add` refuses a role a picker offers. The docs say a rule also undoes changes made by hand in Discord or by other bots.
- **`roles.rules` and `roles.rule_remove`** list and remove rules. `rule_remove` is `write`, as `roles.stop` is, so a default token can stop a runaway rule. `roles.rules` shows each rule's last run and flags a rule whose labels' plugin is off, since it then changes nothing.

## Part 3: the wom plugin

A hub plugin, `wom`, in its own repo (teabot-dev/teabot-wom). It depends on no other plugin.

### What it keeps

- **People:** one table, one row per person. A person may have a Discord member, and may have a note, such as a name for one who isn't on Discord. Accounts belong to a person. A member has at most one person, and keeps it when they unlink their last account.
- **Accounts:** one row per WOM player the group has had or an admin has linked, keyed by WOM's player `id`: the current name, past names, account type, status, in-game rank, and whether it is in the clan, away or left. Everything is kept per server, as every plugin table is; two servers following one group each fetch it, which WOM's limits allow for hundreds of servers.
- **Join dates,** per account:
  - `joined`, the current stint's, from `clientSyncJoinedAt`. It moves later only after the account has been `left`; a merge, or an away account coming back, keeps the earlier date, so a rename never restarts a stint;
  - `first_joined`, the earliest join date known for it, from WOM or set by an admin. A later date from WOM never replaces it; `wom.set_joined` can, to correct a wrong import.

  A membership without `clientSyncJoinedAt` has no join date. The plugin never guesses one from `createdAt`, which can be years late, and lists the account in `wom.gaps`.
- **Leaving:** an account missing from the group is `away` for a grace period, 7 days by default, before it counts as left. While away it still counts as in the clan. A rename keeps the account's ID and join date (S11), so the group's name changes cover it with no look-ups per account. A rename's in-between state, where clan sync has sent the new name before WOM approves the change, then never costs a member their role. A sync in which more than a tenth of the group's accounts, and more than five, go missing is treated as a failed fetch, since a partial clan sync, a wiped group and a clear-out of inactive members look the same: it writes nothing and posts to the admin channel once for the incident. `wom.accept_sync`, destructive, plans the leaves with their names and label changes, and once confirmed takes that group as it is. A new account with no person is listed in `wom.gaps` only after `away_days`, so a rename that briefly shows as a new ID isn't listed as a stranger. Banned accounts are listed there too.
- **Keeping accounts:** an account in a person is kept as long as the person. An unlinked account that left is deleted a year after it left, unless an admin set its `first_joined`, so an imported date survives a long gap. Account names are public, so keeping them costs little.
- **Names:** one function normalises them as OSRS compares names, case-insensitive with spaces, underscores and hyphens the same, for the matcher and for merges.

### Claims

- A member's claim on an account is `waiting`, `confirmed` or `rejected`. A claim names an account in the group, in the clan or away; a name that isn't is turned away at once, with the group's closest names, such as `Green Clog` for "gren clog", and one who joined or renamed recently tries again once WOM has caught up. A claim on an account in someone else's person waits, flagged in `wom.gaps`, and changes nothing until an admin acts. A member makes at most ten claims a day.
- **Confirming a claim** puts the account in the member's person, making one if they have none. When the account belongs to a person without a member, such as one with only a note, confirming merges that person into the member's.
- **Every claim waits for an admin.** A waiting claim shows in `/wom me`, is followed through renames by its WOM ID, and appears in the rank audit marked unconfirmed, so the audit never asks an admin to promote an account on a claim alone. It feeds nothing, no main, dates or labels, until confirmed. The digest lists waiting claims for an admin, or an assistant with one, to confirm in batches.
- Rejected and expired claims are deleted after 30 days.
- **One writer at a time.** The sync and every admin action that changes people take a per-server lock, and each action's plan hashes the person and account rows it touches, so a plan made before a sync comes back stale after it.
- **Main:** the account `/wom me` and `wom.people` show first. Among the person's confirmed accounts in the clan or away, else among all their confirmed accounts: the earliest `first_joined`, then the earliest `joined`, then the lowest WOM ID. Accounts with no date come last. An admin can pin another, which changes what they show only, since labels and the rank audit don't depend on the main.
- **Forgetting a person.** `wom.forget` deletes a person and their links, for a member linked by mistake, so they leave every rule's scope and keep the roles they hold.

### The labels it writes

For each person with a member, from their confirmed accounts. One keyed label job writes them, so two writes never race: a successful sync and every admin action that changes people schedule it a minute out unless one is pending, so a 500-row import makes one write, and it reads the current state when it runs. Every write passes `as_of`, the last successful fetch, so admin actions during an outage never make old data look fresh. A failed fetch writes nothing. When the directory isn't ready, `replace_all` raises `DirectoryUnavailable`, and the sync tries again next hour.

- `linked`, for every member who has a person, accounts or not. A person outlives unlinking every account, so such a member stays inside a rule's scope and loses the clan role.
- `in_clan` when any of their accounts is in the clan or away, and `left` when they have accounts and none is.
- `tenure:<name>`, with a `rank_rule` set, while one of the person's accounts is in the clan or away: the rank the rule gives for their days in the clan, counted as the rank audit counts them, or a higher rank of the rule that one of those accounts holds. The rule is a floor, so a member ranked up early in game keeps that rank in Discord, and a member the game has fallen behind still gets the rank their days give. A rule over tenure roles reads it, and the label moves on the day they reach a step. A person with no join date gets the highest rule rank they hold, if any.
- `rank:<name>` for the in-game rank of each confirmed account in the clan or away, so a rename's in-between state keeps the rank. A rank rule's steps are ordered highest first, so the highest rank a person holds wins with no rank order in the plugin, and separate rules over different ranks, such as staff ranks and a tenure ladder, each find their own. Unlinking an account can only remove labels, so it never raises anyone.

### Keeping in step with WOM

- One recurring job, hourly, per server with a group:
  - `GET /groups/:id`: every membership, with each player's current name and `status`. Comparing it with the last run gives joins, leaves, rank changes and renames of current members. Its names win.
  - `GET /groups/:id/name-changes`: past names and merges. Paging stops at the first page older than the newest change already seen less 30 days, and changes are deduplicated by ID, so one approved late isn't missed. The first sync pages through all of them.
- **Merges:** when a rename is approved for a name WOM already tracked, WOM keeps the renamed account's ID and archives and deletes the player that held the new name. The change carries only the survivor's ID, so the plugin finds the vanished account by matching the change's `newName` against the names it stored. It moves that account's person, claims and earlier `first_joined` onto the survivor, and lists the merge in `wom.gaps`.
- **Archives:** WOM can archive a player, renaming its ID to `archive#####` with `status: archived`, and make a new ID under the old name, which takes over its recent memberships. The plugin treats an archived status, or an `archive` name, as an event for `wom.gaps`, never as a rename. It suggests the new ID to the same person, and keeps the old account's dates.
- **Clan sync's age:** no field records the last clan sync (S11), so `wom.gaps` shows the group's last change, from its `updatedAt`, and raises no flag on it.
- The plugin never asks WOM to update players. WOM asks for that every few hours at most, and clan sync already does it.
- **Limits:** without a key, 20 requests a minute, 500 an hour and 3,000 a day per address, from WOM's docs; with a key, 100 a minute and 20,000 a day. Answers carry only the minute's window in their headers (S11). The two hourly requests cost 48 a day per server, before `wom.link` look-ups. The plugin paces its own WOM requests per instance. The official Teabot runs with `TEABOT_PLUGIN_WOM_API_KEY` from the start, from WOM's Discord.
- `Plugin(hosts=["api.wiseoldman.net"])`, with a descriptive User-Agent naming the project, `teabot-wom/<version> (+repo URL)`, as WOM asks, and nothing naming the operator.
- **A rename WOM missed:** `/wom me` links to WOM's own name-change form, where anyone can submit one. The plugin sends nothing to WOM, so members' input never spends the instance's key.

### Guessing links

The matcher runs after each sync and when someone joins Discord.

- It matches a member's nickname, display name or username against accounts' current and past names, by the one normalising function.
- The evidence says which name matched, current or past.
- A suggestion is listed for an admin in `wom.claims`. It is never recorded on its own, since the member made no claim.
- Patterns for alts, such as "Iron X", wait until real data shows they're worth it. Members linking their own alts with the button is the main route.

### Actions

For members:

| Action | What it does |
|---|---|
| `/wom claim names:` | Claim up to five accounts, a main and its alts, as names one per line or separated by commas; the "Link accounts" button opens the same action. Each claim waits for an admin. A name not in the group is turned away, with the closest names in it. |
| `/wom drop name:` | Drop one of your accounts or claims, at once. |
| `/wom me` | Your accounts, your main and your join dates, as the plugin sees them, with a link to WOM's name-change form. |

For admins, also over MCP and the service API:

| Action | Risk | What it does |
|---|---|---|
| `wom.claims` | read | Waiting claims and suggestions, with their evidence. |
| `wom.confirm` | destructive | Confirm claims, up to ten at once. Its plan lists each member's label changes, since through rules they move Discord roles. |
| `wom.reject` | write | Reject claims. |
| `wom.link` | destructive | Put up to 50 accounts in people directly, refusing one already in another person, which `wom.merge` handles, with their `first_joined` when known, such as from a spreadsheet, with a plan of the label changes. Any WOM player, looked up by name when outside the group. |
| `wom.unlink`, `wom.forget` | destructive | Take an account out of a person, or delete a person, with a plan of the label changes. |
| `wom.merge`, `wom.split` | destructive | Merge two people, or move accounts out of a person into a new one, with a plan of the label changes. Merging two people who both have a member is refused. |
| `wom.pin_main`, `wom.set_joined` | destructive | Pin a person's main, or set an account's `joined` or `first_joined` by hand, with a plan of the label changes. |
| `wom.people` | read | People, their accounts, main and dates; filterable by "no member", "in clan", "away" and "left". |
| `wom.gaps` | read | What needs a person: clan accounts with no member, holders of the clan role with no confirmed account, competing claims, merges, archives, missing join dates, a stale clan sync, and in-game ranks that don't fit the rank rule. |

### Server config

- `group_id`: the WOM group.
- `clan_role`: the Discord role that marks clan members, for `wom.gaps`.
- `rank_rule`: optional, for the rank audit. `count_from`, either `first_joined` (a person's earliest `first_joined`, gaps included) or `current_stint` (the earliest `joined` among their accounts in the clan or away), and `ranks`, the ranks it governs, in order, each with its days in the clan.
- `away_days`: the grace period before an account counts as left, 7 by default.
- `link_channel`, `admin_channel`: where the link post and the weekly digest go.

### The rank audit

In-game ranks can't be set through WOM, so the plugin reports them, for a server with a `rank_rule`. Each run it works out every person's days in the clan from `count_from` and today, and the rank `rank_rule` gives for that. `wom.gaps` and the weekly digest list each account holding one of the rule's ranks below that, such as an alt still at the lowest rank while its main has five years. The rule is a floor: an account ranked above it, such as a member ranked up early, is left alone, as are accounts holding a rank outside the rule, such as a staff rank or one for inactive members. A person with a missing join date is listed as such, never ranked on a guess. An admin fixes the game.

Discord follows the rule through role rules on `tenure:` labels, and the game's own ranks, such as staff ranks, through `rank:` labels. The rank rule lives in one place, and the audit keeps the game in step with it.

### Data and privacy

- Account names are public, through WOM and the hiscores. The link to a Discord member is the new personal data.
- A person's member is a `teabot.member_id`, as is a claim's, so `teabot users delete` removes them. A note is written by admins about someone not on Discord, is deleted with its person, and the privacy policy says so.
- Every action that changes people is destructive, so an assistant on a default token can't move roles through labels, and an assistant confirming claims shows each plan, including member-written names as untrusted text.
- A member sees their own links with `/wom me`, and admins see everyone's.
- The plugin declares `reads=["members"]` for the matcher and `writes=["labels"]`.

## Part 4: Tea's setup

Tea ranks by time in the clan, with a person's oldest account setting the rank of all their accounts. That is Tea's `rank_rule`, counting from the current stint (`count_from: current_stint`).

Everyone has the same tenure rule, and staff have a rule of their own besides it. A staff member holds an in-game staff rank on a few of their accounts, the ones they play most, and any one of them is enough for the Discord staff role. Tenure follows days in the clan against the days Tea sets for each rank, through `tenure:` labels, and staff follows the in-game staff ranks, through `rank:` labels. Fred has three accounts, one holding Silver Key, a staff rank, and has been in the clan long enough for Blue Gem. In Discord he holds Silver Key, Blue Gem and Clan Member, whatever ranks his accounts show in game.

### Step 1: load what's known (one evening, with an assistant)

1. Enable `wom` with Tea's group ID and `rank_rule`, and let the first sync run. Linking a member's main and alts comes before any rank changes, in game or in Discord, since an alt's days count from its person's earliest join.
2. Work through `wom.claims` with an assistant over MCP, reading each batch's plan. Nothing kept by hand is imported; members' claims and the matcher's suggestions start from scratch, and hand-kept records can be checked against them later. The rules come at step 4, so this meets no cap; later, a large batch of confirms may be held, and an admin lets it through with `cap_once=`.
3. Once linking has settled, check `wom.gaps`, set missing join dates, and make the in-game promotions the audit lists, so the rules' first run starts from a corrected game.
4. On the site, make the role rules, scoped to linked members (`labels_from: wom, labels_any: [linked]`), so unlinked members keep their roles:
   - **Membership:** one rule with two steps, Clan Member on `in_clan`, then Friends on `left`, so a linked member holds exactly one. Friends is also for guests who never joined; they aren't linked, so they're outside the scope and keep the Friends an admin gives them by hand.
   - **Tenure:** one rule over the tenure rank roles, a step per tenure rank on `tenure:<rank>`, highest first, with a cap that fits a promotion night. A linked member holds at most one tenure role.
   - **Staff:** one rule over the staff rank roles, a step per staff rank on `rank:<rank>`, highest first. A linked member holds at most one staff role.
5. Note which admins confirmed the rules, since their grants end when those admins lose the rights, and a second admin renews them by running the same action again.

### Step 2: the link channel

- `#link-your-rsn`, with one post:
  - what linking does for the member, as it stands when posted: at first, that admins can see who is who; once the rules are on, the post is edited to say their Discord rank is kept matching the clan;
  - the "Link accounts" button, with every account at once, alts included;
  - that each account waits for an admin, usually a day.
- The bot edits the post's count of linked members each day, so others see it working, naming nobody.
- New members get a line in the welcome message pointing to it.
- After step 1, the post is announced once with a single @Clan Member mention.

### Step 3: the routine

| Who | What | How often |
|---|---|---|
| The wom plugin | Syncs with WOM, follows renames, keeps labels | Hourly |
| The roles plugin | Applies role rules when labels or roles change, holding back large changes | As changes happen, and daily |
| The wom plugin | Posts a digest to the admin channel: waiting claims, competing claims, leavers, merges, archives, rank mismatches, a stale clan sync | Weekly |
| An admin, or an assistant with one | Settles claims, works through `wom.gaps`, releases a held rule run if it was right | Weekly |
| An admin, in game | Fixes ranks the audit lists | When the digest lists any |
| Members | Link accounts, add new alts, report renames WOM missed on WOM's form | When something changes |

### How much is automated

- **Automatic:** syncing, renames, labels, and role rules within their caps.
- **Confirmed by a person:** every claim, and held rule runs.
- **Left to members:** saying which accounts are theirs, which neither WOM nor Discord can know.
- **Left to admins:** in-game ranks, and claims that don't fit the evidence.

## Decided for Tea

1. Time in the clan counts from the current stint.
2. Hand-kept links are not imported; claims start from scratch.
3. Friends is for both people who left the clan and guests who never joined.
4. Tenure and staff are separate rules. Tenure comes from days in the clan against Tea's days for each rank, as a floor: a member ranked up early keeps their rank. Any account with an in-game staff rank gives its person that staff rank's Discord role.
5. Tea's ladder, in 6-month steps up to 3 years and yearly after: specialist at 1 week (or earlier, for an introduction post), pyromancer at 1 month, prodigy 3 months, ignitor 6 months, artisan 9 months, firestarter 1 year, legacy 1.5 years, sapphire 2 years, emerald 2.5 years, ruby 3 years, diamond 4 years, dragonstone 5 years.
6. Burnt is for members not seen online in a long time, set by hand in game, and outside the ladder.
7. Main and alt linking is done before any ranks change.
8. Tea's WOM group is 651.

## Order of work

The linking campaign takes weeks of members' time, so it starts as early as it can. Every SDK piece comes before the plugin part that uses it.

1. Buttons that ask, member labels, the filter fields, several filters at once, `writes=["labels"]` and `on_labels_changed`. Labels change nothing in Discord on their own, so they ship before any rule reads them.
2. The wom plugin's linking, labels and rank audit: sync, people, claims, `/wom`, `wom.gaps`, the admin actions with their label plans, the link channel and the digest. Tea's step 1 up to its role rules, and step 2, start here.
3. Role rules in the roles plugin.
4. Tea's rules.

## Decisions this changes

- **D45** gains member labels, `on_labels_changed` and buttons that ask.
- **Role pickers** refuse a role a rule owns.
- **Role changes (design/role-changes.md):** rules and the `change` job share one pace per server, with rules first. Rules make changes with no member confirming each run, under a standing grant.
- **The member directory (design/member-directory.md):** labels become directory data, keyed by member ID.
- **D52**, the privacy policy: game-account links and labels.
