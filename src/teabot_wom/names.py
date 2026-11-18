"""Account names as OSRS compares them, for matching claims, renames and members' Discord names."""

import re

SEPARATORS = re.compile(r"[\s_\- ]+")
NAME_MAX = 12


def key(name: str) -> str:
    """A name as OSRS compares names: lower case, with spaces, underscores and hyphens the same."""
    return SEPARATORS.sub(" ", name).strip().lower()


def names_in(text: str) -> list[str]:
    """Account names from what a member typed: one per line, or separated by commas, without repeats."""
    found: list[str] = []
    for part in re.split(r"[,\n]", text):
        name = part.strip()
        if name and key(name) not in {key(f) for f in found}:
            found.append(name)
    return found


def words(text: str) -> list[str]:
    """`text` as its OSRS-compared words, for finding a name as whole words inside a nickname."""
    return key(text).split(" ")


def contains(text: str, name: str) -> bool:
    """Whether `name` appears in `text` as whole words, compared as OSRS compares names."""
    inside, wanted = words(text), words(name)
    return any(inside[i : i + len(wanted)] == wanted for i in range(len(inside) - len(wanted) + 1))
