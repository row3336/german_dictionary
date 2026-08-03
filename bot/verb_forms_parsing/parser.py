"""Proxy helpers shared by the VF (Netzverb) fetching flow.

What is left of the old verbformen.com verify pipeline (removed in T4, see
log.md 2026-07-11): the proxy setting key and its normalize/mask helpers, used
by the .de flow (de_fetch) and the parse_VF proxy mini-menu in settings.
DB-persisted names (vf_checks table, vf_* setting keys) are intentionally
kept as-is in the database.
"""

import re

from ..storage import _safe_text

VF_PROXY_KEY = "vf_proxy"


def _mask_proxy(proxy_url):
    token = _safe_text(proxy_url).strip()
    if "@" not in token:
        return token
    left, right = token.rsplit("@", 1)
    if "://" in left and ":" in left:
        scheme, creds = left.split("://", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{right}"
    return f"***@{right}"


def _normalize_proxy_input(raw):
    """Normalize proxy input to a curl-compatible URL.

    Accepts the compact ``host:port:user:pass`` / ``host:port`` format from proxy
    providers. A bare value (no scheme) defaults to ``socks5`` (back-compat). To
    use an http (or socks5h) proxy, prefix the scheme: ``http://host:port:user:pass``
    -> ``http://user:pass@host:port``. A full URL with ``user:pass@host:port`` is
    kept as-is.
    """
    token = _safe_text(raw).strip()
    if not token:
        return ""
    scheme = "socks5"  # default for a bare host:port[:user:pass]
    m = re.match(r"^(https?|socks5h?)://(.*)$", token, re.IGNORECASE)
    if m:
        scheme = m.group(1).lower()
        rest = m.group(2)
        if "@" in rest:  # already user:pass@host:port -> valid URL, keep it
            return token
        token = rest  # reparse the compact form below with the explicit scheme
    elif "://" in token:
        return token  # some other scheme already present -> keep as-is
    parts = token.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port, user = parts[0].strip(), parts[1].strip(), parts[2].strip()
        password = ":".join(parts[3:]).strip()
        if host and port and user and password:
            return f"{scheme}://{user}:{password}@{host}:{port}"
    if len(parts) == 2 and parts[1].isdigit():
        host, port = parts[0].strip(), parts[1].strip()
        if host and port:
            return f"{scheme}://{host}:{port}"
    # unmatched: bare stays as-is (back-compat); explicit scheme is re-applied
    return token if scheme == "socks5" else f"{scheme}://{token}"
