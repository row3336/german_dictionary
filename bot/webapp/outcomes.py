"""Wire codes for a session that has nothing to ask (miniapp.md §7.2, §7.3).

Their own module because all three flows produce them and the routes read them
back: putting them in either place would make one import the other, and the
client screens keyed off these strings are the one thing that must not drift.
"""

# Words exist, but none of them is due right now.
EMPTY_NO_DUE = "no_due"

# The active topic filter holds no words at all (spec #8) — a different screen,
# because the fix is different: pick other topics rather than come back later.
EMPTY_NO_WORDS = "no_words"
