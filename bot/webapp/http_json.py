"""Reading a JSON request body, once, for every route that takes one.

Split out of session_api.py when the quiz routes arrived: both modules need it,
and session_api needs to import the quiz routes back, which a shared helper living
in either of them would turn into an import cycle.
"""

import json

from aiohttp import web

BAD_REQUEST = "bad_request"


async def json_body(request):
    """Parse a JSON object body, or refuse with 400.

    An empty body is treated as an empty object: several of these endpoints have no
    required field, and demanding `{}` from the client buys nothing.
    """
    raw = await request.text()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": BAD_REQUEST}), content_type="application/json"
        )
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": BAD_REQUEST}), content_type="application/json"
        )
    return body
