"""Shared telegram stub for tests that import modules depending on PTB.

PTB isn't installed in the test env, so `install()` registers a minimal superset
stub for `telegram`, `telegram.error`, and `telegram.ext`. Idempotent and
augmenting: it only fills in missing attributes, so it composes with any partial
stub another test may have installed first, regardless of run order.
"""

import sys
import types


def install():
    """Register/augment the telegram stub modules. Safe to call repeatedly."""
    tg = sys.modules.get("telegram") or types.ModuleType("telegram")
    sys.modules["telegram"] = tg
    # ReplyKeyboardMarkup keeps .rows (some keyboard tests inspect it).
    if not hasattr(tg, "ReplyKeyboardMarkup"):
        tg.ReplyKeyboardMarkup = type(
            "ReplyKeyboardMarkup", (),
            {"__init__": lambda self, rows=None, **k: setattr(self, "rows", rows)},
        )
    for _n in ("ReplyKeyboardRemove", "Update", "InlineKeyboardButton", "InlineKeyboardMarkup",
               "LabeledPrice"):
        if not hasattr(tg, _n):
            setattr(tg, _n, type(_n, (), {"__init__": lambda self, *a, **k: None}))

    err = sys.modules.get("telegram.error") or types.ModuleType("telegram.error")
    if not hasattr(err, "TelegramError"):
        err.TelegramError = type("TelegramError", (Exception,), {})
    for _n in ("NetworkError", "RetryAfter", "TimedOut", "Forbidden"):
        if not hasattr(err, _n):
            setattr(err, _n, type(_n, (err.TelegramError,), {}))
    sys.modules["telegram.error"] = err
    tg.error = err

    ext = sys.modules.get("telegram.ext") or types.ModuleType("telegram.ext")
    if not hasattr(ext, "Application"):
        ext.Application = type("Application", (), {})
    if not hasattr(ext, "ContextTypes"):
        ext.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
    if not hasattr(ext, "ConversationHandler"):
        ext.ConversationHandler = type("ConversationHandler", (), {"END": -1})
    # Handlers / limiter referenced by bot.main's imports (only instantiated inside
    # main(), so no-op constructors suffice for importing the module under test).
    for _n in ("AIORateLimiter", "Application", "CallbackQueryHandler", "CommandHandler",
               "MessageHandler", "PreCheckoutQueryHandler"):
        if not hasattr(ext, _n):
            setattr(ext, _n, type(_n, (), {"__init__": lambda self, *a, **k: None}))
    if not hasattr(ext, "filters"):
        ext.filters = types.SimpleNamespace()
    sys.modules["telegram.ext"] = ext
    tg.ext = ext
