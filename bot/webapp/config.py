"""Mini App settings, kept out of bot/app_config.py on purpose.

The web layer is developed on its own branch and is wired into the bot only in a
final, separate commit (see miniapp.md §3.3). Holding its ENV keys here means the
branch touches no file the bot branch also edits.
"""

import os

# §9.2 п.4: the app binds the loopback interface and nothing else — nginx is the
# only process allowed to face the network. Not configurable by design; an ENV key
# here would be one typo away from exposing the API directly.
WEBAPP_HOST = "127.0.0.1"
WEBAPP_PORT_DEFAULT = 8081

# §6.1 п.2: initData older than this is refused even with a valid signature, so a
# leaked query string stops working within a day.
WEBAPP_INIT_DATA_TTL_SECONDS = 24 * 60 * 60

_TRUE_VALUES = {"1", "true", "yes", "on"}


def webapp_enabled():
    """Master switch (§3.3). Off by default: deploying this code changes nothing.

    Rollback is flipping the ENV back to 0 and restarting — no code revert, no
    migration, because the Mini App owns no schema of its own.
    """
    return os.getenv("WEBAPP_ENABLED", "0").strip().lower() in _TRUE_VALUES


def get_webapp_port():
    """Loopback port behind nginx. Falls back to the default on garbage input."""
    try:
        return int(os.getenv("WEBAPP_PORT", str(WEBAPP_PORT_DEFAULT)))
    except ValueError:
        return WEBAPP_PORT_DEFAULT
