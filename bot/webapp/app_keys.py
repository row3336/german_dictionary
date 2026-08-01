"""aiohttp application keys, in one place with no imports of our own.

Every web module needs some of these, and several need each other's routes, so
keeping the keys in a leaf module is what stops the import graph from becoming a
cycle as more of the API lands.
"""

from aiohttp import web

# The bot token, used to verify initData signatures. Never logged, never served.
BOT_TOKEN_KEY = web.AppKey("bot_token", str)

# The quiz-session registry (bot/webapp/sessions.py): one per running server.
REGISTRY_KEY = web.AppKey("session_registry")

# Access to the running PTB Application, or a disabled stand-in when the web layer
# runs on its own (bot/webapp/bot_bridge.py).
BRIDGE_KEY = web.AppKey("bot_bridge")
