"""Telegram Mini App backend (claude_docs/archive/specs/miniapp.md).

Lives in the bot's own process and event loop: one copy of the algorithms, one
database handle. Nothing here is imported at bot start unless WEBAPP_ENABLED=1,
so the web extra (aiohttp) stays an optional dependency at runtime.
"""
