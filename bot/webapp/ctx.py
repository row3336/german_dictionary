"""WebContext — the duck-typed stand-in for PTB's context (miniapp.md §5.2).

The session helpers in bot/common/session.py take a `context` as their first
argument but only ever read and write `context.user_data`; nothing in that module
touches `.bot`, `.application`, `.job_queue`, `.chat_data` or `.bot_data`. So a
plain holder of one dict is enough to call them verbatim from a web request, which
is the whole point: the Mini App must not own a second copy of the retry queue,
the FSRS cycle or the session counters (§5.1 — a second copy means the bot and the
app eventually disagree about the same answer, and the user is the one who finds out).

tests/test_webapp_ctx.py pins that promise down by driving the real helpers through
this object.
"""


class WebContext:
    """Minimal stand-in for telegram.ext.CallbackContext inside web requests.

    Holds exactly one attribute on purpose. If a future helper needs something
    else from the context, that is a signal to split its pure part out into
    bot/common/ (§5.3) rather than to grow this class.
    """

    __slots__ = ("user_data",)

    def __init__(self, user_data=None):
        self.user_data = {} if user_data is None else user_data

    def __repr__(self):
        return f"WebContext(keys={sorted(self.user_data)})"
