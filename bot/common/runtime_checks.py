"""Startup guards for the runtime environment.

A too-old python-telegram-bot does not fail at import time: the bot starts,
serves menus, and only blows up inside individual handlers (prod, 2026-07-04:
10x `TypeError: reply_text() got an unexpected keyword argument
'message_effect_id'` and `InlineKeyboardButton.__init__() got an unexpected
keyword argument 'style'`, which users saw as "Something went wrong"). Failing
loudly at startup turns that into one obvious line in the journal.
"""

import telegram

# Bumped together with the pin in requirements.txt. Features that need it:
# `message_effect_id` on sends (spec #9) and `style=` on InlineKeyboardButton
# (Bot API 9.4, spec #8).
MIN_PTB_VERSION = (22, 0)


def _installed_ptb_version():
    """Installed PTB version as a comparable tuple of ints."""
    info = getattr(telegram, "__version_info__", None)
    if info:
        # __version_info__ carries pre-release fields too; the numeric head is enough.
        return tuple(int(part) for part in tuple(info)[:2] if isinstance(part, int))
    parts = []
    for chunk in telegram.__version__.split(".")[:2]:
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def require_supported_ptb():
    """Raise SystemExit when the installed PTB is older than the pinned minimum."""
    installed = _installed_ptb_version()
    if installed and installed < MIN_PTB_VERSION:
        want = ".".join(str(p) for p in MIN_PTB_VERSION)
        raise SystemExit(
            f"python-telegram-bot {telegram.__version__} is too old: >= {want} required. "
            "Reinstall the venv with `pip install -r requirements.txt`."
        )
    return telegram.__version__
