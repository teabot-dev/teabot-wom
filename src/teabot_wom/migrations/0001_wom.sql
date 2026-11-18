-- People, their Wise Old Man accounts, members' claims on accounts, and each server's sync with its WOM group.

-- A person: a Discord member, or someone not on Discord, named by an admin's note. Their accounts point at them.
CREATE TABLE people (
    guild_id bigint NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    user_id teabot.member_id,
    note text,
    -- The account an admin chose as the person's main, in place of the earliest joined.
    pinned_main bigint,
    PRIMARY KEY (guild_id, id)
);
CREATE UNIQUE INDEX people_member ON people (guild_id, user_id) WHERE user_id IS NOT NULL;

-- A WOM player the group has had, or an admin linked, keyed by WOM's player ID, which a rename keeps.
CREATE TABLE accounts (
    guild_id bigint NOT NULL,
    player_id bigint NOT NULL,
    name text NOT NULL,
    -- The name as OSRS compares names: lower case, with spaces, underscores and hyphens the same.
    key text NOT NULL,
    past_names text[] NOT NULL DEFAULT '{}',
    type text,
    status text,
    rank text,
    -- in: in the group; away: missing from it, within the grace period; left: gone.
    state text NOT NULL CHECK (state IN ('in', 'away', 'left', 'outside')),
    away_since timestamptz,
    left_at timestamptz,
    -- The current stint's join date, and the earliest ever known, which a later date never replaces.
    joined date,
    first_joined date,
    first_joined_by_admin boolean NOT NULL DEFAULT false,
    person_id bigint,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, player_id),
    FOREIGN KEY (guild_id, person_id) REFERENCES people (guild_id, id) ON DELETE SET NULL (person_id)
);
CREATE INDEX accounts_key ON accounts (guild_id, key);
CREATE INDEX accounts_person ON accounts (guild_id, person_id);

-- A member saying an account is theirs, which waits for an admin.
CREATE TABLE claims (
    guild_id bigint NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    user_id teabot.member_id NOT NULL,
    name text NOT NULL,
    key text NOT NULL,
    state text NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'confirmed', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    PRIMARY KEY (guild_id, id)
);
CREATE INDEX claims_key ON claims (guild_id, key);

-- The server's sync with its group: the last good fetch, and a sync held because too many accounts went missing.
CREATE TABLE sync_state (
    guild_id bigint PRIMARY KEY,
    group_id bigint,
    fetched_at timestamptz,
    group_updated_at timestamptz,
    held_since timestamptz,
    held_missing integer,
    -- How many missing accounts an admin accepted with wom.accept_sync, for the next sync.
    accepted_missing integer NOT NULL DEFAULT 0,
    newest_change timestamptz
);

-- Name changes already read, so a page read again changes nothing.
CREATE TABLE seen_changes (
    guild_id bigint NOT NULL,
    change_id bigint NOT NULL,
    PRIMARY KEY (guild_id, change_id)
);

-- What happened that an admin should know of, such as a merge or an archive, for wom.gaps and the digest.
CREATE TABLE notices (
    guild_id bigint NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    kind text NOT NULL,
    text text NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, id)
);
