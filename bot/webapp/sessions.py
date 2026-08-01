"""Quiz-session registry and the bot/app mutual exclusion (miniapp.md §4).

The problem this solves (§4.1): a quiz session is not persistent state. The current
word, the retry queue, the weak drill and the FSRS cycle all live in PTB's
`context.user_data` — in memory, gone on restart — while only the long-term
progress is in the database. If the app kept a second, independent queue, the same
word would be asked twice, the retry gap would drift, and `input_err` could roll
back something other than what the user just answered. None of that crashes; it
quietly corrupts, which is the worst failure mode this project already knows.

So there is exactly one active session per user, and the last client in wins
(§4.2). Everything here is deliberately free of aiohttp and of telegram: the rules
are the part that has to be provably right, and they are easier to prove when they
can be driven directly from a test.

Nothing is written to the database. A session is ephemeral by design — a restart
kills the bot's sessions and the app's sessions alike, which is symmetric and
predictable, and it means this whole feature needs no migration (§12 risk 10).
"""

import time
import uuid

from .ctx import WebContext

CLIENT_APP = "app"
CLIENT_BOT = "bot"

# §4.3: idle sessions are swept so a long-lived process does not accumulate one
# stale WebContext per user who ever opened the app.
SESSION_TTL_SECONDS = 2 * 60 * 60


class SessionError(Exception):
    """Base for the 409 conditions in §7.7. `code` is what the client receives."""

    code = "session_error"


class SessionTakenOver(SessionError):
    """The bot (or another app instance) took the session over — §4.2 п.3."""

    code = "session_taken_over"


class StaleQuestion(SessionError):
    """The answer arrived with a spent or unknown token — §4.4."""

    code = "stale_question"


class WebSession:
    """One live quiz session belonging to one user.

    `ctx` is the duck-typed context the bot's own helpers read and write (§5.2), so
    the queues inside a web session have exactly the shape the bot would build.
    """

    __slots__ = (
        "user_id",
        "session_id",
        "mode",
        "word_type",
        "ctx",
        "question_token",
        "created_at",
        "touched_at",
    )

    def __init__(self, user_id, mode, word_type=None, now=0.0):
        self.user_id = user_id
        self.session_id = str(uuid.uuid4())
        self.mode = mode
        self.word_type = word_type
        self.ctx = WebContext()
        self.question_token = ""
        self.created_at = now
        self.touched_at = now

    def __repr__(self):
        return f"WebSession(user={self.user_id}, mode={self.mode!r}, id={self.session_id[:8]})"


class SessionRegistry:
    """Who owns each user's session right now, and what that session holds.

    Two maps rather than one: a user can be marked as "answering in the chat"
    without the app ever having had a session, and an app session must be findable
    even in the instant before its owner flag is read.
    """

    def __init__(self, ttl_seconds=SESSION_TTL_SECONDS, time_source=time.time):
        self._sessions = {}
        self._clients = {}
        self._ttl = ttl_seconds
        self._now = time_source

    # -- ownership ---------------------------------------------------------

    def active_client(self, user_id):
        """`'app'`, `'bot'`, or None if this user has not started anywhere yet."""
        record = self._clients.get(user_id)
        return record["client"] if record else None

    def _set_client(self, user_id, client):
        self._clients[user_id] = {"client": client, "since": self._now()}

    # -- app side ----------------------------------------------------------

    def start(self, user_id, mode, word_type=None):
        """Open a session for the app, replacing whatever was open before.

        Covers both directions of §4.2: it takes the session away from the bot, and
        it takes it away from an earlier app session — a second tab or a second
        device is the same event as the bot answering, and gets the same 409.
        """
        session = WebSession(user_id, mode, word_type, now=self._now())
        self._sessions[user_id] = session
        self._set_client(user_id, CLIENT_APP)
        return session

    def require(self, user_id, session_id=None):
        """Return this user's live session or raise SessionTakenOver.

        `session_id` is checked when the client supplies it, so a stale tab that
        still remembers an older session cannot keep answering into the newer one.
        """
        session = self._sessions.get(user_id)
        if session is None or self.active_client(user_id) != CLIENT_APP:
            raise SessionTakenOver(f"no live app session for {user_id}")
        if session_id is not None and session_id != session.session_id:
            raise SessionTakenOver(f"session {session_id} was superseded")
        session.touched_at = self._now()
        return session

    def stop(self, user_id):
        """Close the session from the app side (the user pressed finish)."""
        self._sessions.pop(user_id, None)
        self._clients.pop(user_id, None)

    # -- bot side ----------------------------------------------------------

    def claim_for_bot(self, user_id):
        """The user answered in the chat — the chat wins from now on (§4.2 п.2).

        Returns True when an app session was actually taken over, so the caller can
        tell "the user came back to the bot" from "the user was in the bot all
        along" and stay quiet in the second case.
        """
        had_app_session = (
            self._sessions.pop(user_id, None) is not None
            and self.active_client(user_id) == CLIENT_APP
        )
        self._set_client(user_id, CLIENT_BOT)
        return had_app_session

    # -- question tokens (§4.4) -------------------------------------------

    def issue_question(self, session):
        """Mint the one-shot token that the next answer has to carry."""
        session.question_token = str(uuid.uuid4())
        session.touched_at = self._now()
        return session.question_token

    def consume_question(self, session, token):
        """Spend the token, or refuse the answer.

        This is what stops a double tap or a network retry from writing the same
        answer into `progress` and `quiz_events` twice — on a flaky mobile
        connection that silently skews the statistics (§12 risk 2).
        """
        if not session.question_token or token != session.question_token:
            raise StaleQuestion("question token does not match the outstanding question")
        session.question_token = ""
        session.touched_at = self._now()

    # -- housekeeping ------------------------------------------------------

    def sweep(self):
        """Drop sessions idle for longer than the TTL. Returns how many went."""
        cutoff = self._now() - self._ttl
        stale = [uid for uid, s in self._sessions.items() if s.touched_at <= cutoff]
        for user_id in stale:
            self._sessions.pop(user_id, None)
            # The ownership record goes too: a swept session must not leave the user
            # marked as "in the app" forever.
            if self.active_client(user_id) == CLIENT_APP:
                self._clients.pop(user_id, None)
        return len(stale)

    def __len__(self):
        return len(self._sessions)
