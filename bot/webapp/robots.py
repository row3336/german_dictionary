"""Keeping the Mini App out of search results and crawler archives.

Obscurity is not the protection it looks like: the moment a certificate is issued
for a hostname it lands in the public certificate transparency logs, so the
address is discoverable without a single link pointing at it.

Two directives, both travelling with the code rather than living in whichever CDN
or reverse proxy happens to be in front of the app today - a dev tunnel now, a
bought domain later:

  * `/robots.txt` disallows everything, which is what well-behaved crawlers read
    before fetching anything else;
  * `X-Robots-Tag` repeats it per response, and that is the one that covers the
    static assets and the API replies - robots.txt only asks a crawler not to
    fetch a URL, while the header tells it not to index what it already has.
"""

from aiohttp import web

routes = web.RouteTableDef()

# noindex keeps the response out of the index; nofollow stops a crawler from
# treating anything it finds inside as a new URL to visit.
ROBOTS_TAG = "noindex, nofollow"

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


@routes.get("/robots.txt")
async def robots_txt(request):
    """Served without auth: a crawler has no initData and must still read this."""
    return web.Response(text=ROBOTS_TXT, content_type="text/plain")


@web.middleware
async def robots_middleware(request, handler):
    """Stamp X-Robots-Tag on every response, successful or not.

    Registered outermost so it also reaches the bodies other middleware produce:
    a 401 from the access gate or a 404 from the router is still a page a crawler
    can hold on to.
    """
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        # aiohttp's own 404/405 arrive as exceptions, and the response the client
        # sees is the exception itself - so the header goes on before it flies on.
        exc.headers["X-Robots-Tag"] = ROBOTS_TAG
        raise
    response.headers["X-Robots-Tag"] = ROBOTS_TAG
    return response
