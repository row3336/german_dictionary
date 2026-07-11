"""verbformen.de fetch primitives (spec #2, Step 3b).

Low-level HTTP only: build the summary URL, pull the linked conjugation page URL
out of a summary, and fetch a URL through the configured (rotating http) proxy
with browser-like headers + User-Agent rotation. Pacing (the 20-50s slow-drip
delay) and orchestration live in the job runner, not here.
"""

import random
import re
import subprocess
from urllib.parse import quote

from ..app_config import get_db_path
from ..storage import _safe_text, get_setting
from .parser import VF_PROXY_KEY, _normalize_proxy_input

BASE = "https://www.verbformen.de"
# Netzverb's German shell hosts dedicated pages for function words (prepositions,
# adverbs, conjunctions) that verbformen.de's `?w=` search does NOT — there `?w=aus`
# resolves to the noun homonym "das Aus". These type pages use the same markup our
# extractors already read (translations + CEFR badge), so a typed lookup lands on
# the right word instead of a homonym.
VERBEN_BASE = "https://www.verben.de"

# DB word type -> verben.de URL path segment. Only function-word types that need
# the disambiguated type page; nomen/verb/adjektiv keep the richer verbformen `?w=`
# summary (its structural extractors — article/plural, degrees, forms — rely on
# that layout, which verben.de does not reproduce).
TYPE_URL_SEGMENTS = {
    "praeposition": "praepositionen",
    "adverb": "adverbien",
    "konjunktion": "konjunktionen",
    "partikel": "partikeln",
}

# Rotate the fingerprint per request ("подмешивать имитацию разных" — owner):
# mobile + desktop across OSes. The rotating proxy changes IP per request too.
_USER_AGENTS = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
)


def configured_proxy():
    """Normalized proxy URL from the vf_proxy setting (may be '')."""
    return _normalize_proxy_input(get_setting(get_db_path(), VF_PROXY_KEY, ""))


def summary_url(word, type_hint=None):
    """The .de summary page URL for a word (query encoded).

    With a `type_hint` that maps to a verben.de type page (preposition/adverb/
    conjunction), return that dedicated page so the lookup is not hijacked by a
    same-spelling noun/verb homonym on verbformen's `?w=` search. Otherwise fall
    back to the verbformen summary search.
    """
    w = quote(str(word or "").strip())
    seg = TYPE_URL_SEGMENTS.get(str(type_hint or "").strip().lower())
    if seg:
        return f"{VERBEN_BASE}/{seg}/{w}.htm"
    return f"{BASE}/?w={w}"


def conjugation_url(summary_html):
    """Absolute URL of the conjugation page linked from a summary, or '' if none.

    The slug is NOT transliterable (können -> koennen 404s), so it is always taken
    from the summary's href rather than constructed.
    """
    m = re.search(r'href="([^"]*/(?:konjugation|conjugation)/[^"]+\.htm)"', summary_html or "")
    if not m:
        return ""
    href = m.group(1)
    return href if href.startswith("http") else BASE + "/" + href.lstrip("/")


def _random_user_agent():
    return random.choice(_USER_AGENTS)


def fetch_url(url, proxy=None, timeout_sec=40):
    """Fetch one URL through the proxy with browser-like headers.

    Returns (ok, http_code, html). ok is False on a transport error, a non-200
    status, or a verbformen throttle page ("Zugriffe" / 429). Never raises — the
    runner decides how to react (retry / count as fetch_error).
    """
    proxy = configured_proxy() if proxy is None else proxy
    cmd = [
        "curl", "-sS", "-L", "--compressed",
        "-A", _random_user_agent(),
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
        "-H", f"Referer: {BASE}/",
        "--max-time", str(int(timeout_sec)),
        "-w", "\n__HTTP__%{http_code}",
    ]
    if proxy:
        cmd.extend(["-x", proxy])
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception:
        return False, 0, ""
    if proc.returncode != 0:
        return False, 0, ""
    body, _, tail = proc.stdout.rpartition("\n__HTTP__")
    code = 0
    if tail.strip()[:3].isdigit():
        code = int(tail.strip()[:3])
    # Sanity marker spans both Netzverb shells (verbformen.de + verben.de); a page
    # missing it is a captcha/error stub, not a real dictionary page.
    low = body.lower()
    is_dict_page = "verbformen" in low or "verben.de" in low or "netzverb" in low
    blocked = code == 429 or "Zugriffe" in body or not is_dict_page
    return (code == 200 and not blocked), code, body


def fetch_word_pages(word, proxy=None, timeout_sec=40, pause=None, type_hint=None):
    """Fetch a word's summary and (if linked) conjugation page.

    Returns (summary_html, conj_html); conj_html is '' when there is no linked
    conjugation page. Raises RuntimeError if the summary fetch fails, so the
    runner marks the item as an error. `pause` (a no-arg callable) is invoked
    between the two HTTP requests so the runner can enforce the slow-drip delay.
    `type_hint` routes function words to their verben.de type page (see
    summary_url); function-word pages have no conjugation link, so conj stays ''.
    """
    proxy = configured_proxy() if proxy is None else proxy
    ok, code, summary = fetch_url(summary_url(word, type_hint), proxy, timeout_sec)
    if not ok:
        raise RuntimeError(f"summary fetch failed (http={code}) for {word!r}")
    conj = ""
    curl = conjugation_url(summary)
    if curl:
        if pause:
            pause()
        c_ok, _c_code, c_html = fetch_url(curl, proxy, timeout_sec)
        conj = c_html if c_ok else ""
    return summary, conj
