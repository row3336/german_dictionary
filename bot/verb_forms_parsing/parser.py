"""verbformen.de parsing/fetching/diff logic + proxy + settings helpers.

Verifies local word forms (articles/plural, prateritum/partizip2) against
verbformen.com. Leaf module: depends only on storage/app_config/settings/
text_utils + stdlib. DB-persisted names (vf_checks table, vf_* setting keys)
are intentionally kept as-is.
"""

import html
import random
import re
import subprocess

from ..app_config import get_db_path
from ..common.settings import _get_int_setting
from ..storage import _safe_text, get_setting, get_word_by_id, update_word_fields
from ..text_utils import normalize_answer as _normalize_answer

VF_PROXY_KEY = "vf_proxy"
VF_SLEEP_KEY = "vf_sleep_sec"
VF_JITTER_KEY = "vf_jitter_pct"


def _vf_settings_snapshot():
    """Return vf parser settings."""
    db_path = get_db_path()
    proxy = _safe_text(get_setting(db_path, VF_PROXY_KEY, "")).strip()
    sleep_sec = _get_int_setting(db_path, VF_SLEEP_KEY, 60)
    jitter_pct = _get_int_setting(db_path, VF_JITTER_KEY, 20)
    return proxy, max(1, sleep_sec), max(0, min(80, jitter_pct))


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


def _vf_parse_noun(html_text):
    text = html_text or ""
    article = ""
    plural = ""
    m_article = re.search(r'The article is "((?:der|die|das)\s+[^"]+)"', text, flags=re.IGNORECASE)
    if m_article:
        phrase = m_article.group(1).strip()
        parts = phrase.split()
        if parts:
            article = parts[0].lower()
    m_plural = re.search(r'nominative plural "([^"]+)"', text, flags=re.IGNORECASE)
    if m_plural:
        plural = m_plural.group(1).strip()
    if not article or not plural:
        body = re.sub(r"<[^>]+>", " ", text)
        body = html.unescape(body)
        body = re.sub(r"\s+", " ", body).strip()
        if not article:
            m2 = re.search(r'\bThe article is "((?:der|die|das)\s+[^"]+)"', body, flags=re.IGNORECASE)
            if m2:
                article = m2.group(1).split()[0].lower()
        if not plural:
            m2 = re.search(r'\bnominative plural "([^"]+)"', body, flags=re.IGNORECASE)
            if m2:
                plural = m2.group(1).strip()
    return {"article": article, "plural": plural}


def _vf_parse_verb(html_text):
    text = html_text or ""
    pr = ""
    p2 = ""
    m = re.search(r'<p[^>]+id="stammformen"[^>]*>(.*?)</p>', text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        chunk = m.group(1)
        chunk = re.sub(r"<a[^>]*>.*?</a>", " ", chunk, flags=re.IGNORECASE | re.DOTALL)
        chunk = re.sub(r"<[^>]+>", "", chunk)
        chunk = html.unescape(chunk)
        chunk = re.sub(r"\s+", " ", chunk).strip()
        parts = [x.strip() for x in chunk.split("·") if x.strip()]
        if len(parts) >= 2:
            pr = re.sub(r"[^A-Za-zÄÖÜäöüß -]+", "", parts[1]).strip()
        if len(parts) >= 3:
            p2_raw = re.sub(r"[^A-Za-zÄÖÜäöüß -]+", "", parts[2]).strip()
            p2_tokens = [t for t in p2_raw.split() if t]
            if p2_tokens and p2_tokens[0].lower() in {"hat", "haben", "ist", "sein"}:
                p2_tokens = p2_tokens[1:]
            p2 = " ".join(p2_tokens).strip()
    return {"prateritum": pr, "partizip2": p2}


def _vf_fetch_word(base_form, proxy_url, user_agent, timeout_sec=30):
    url = f"https://www.verbformen.com/?w={base_form}"
    cmd = [
        "curl",
        "-L",
        "--compressed",
        "-A",
        user_agent,
        "--max-time",
        str(int(timeout_sec)),
        "--fail-with-body",
    ]
    if proxy_url:
        cmd.extend(["-x", proxy_url])
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "curl failed").strip()[:300])
    return proc.stdout


def _vf_user_agent():
    agents = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    return random.choice(agents)


def _vf_compare(local_entry, remote_data):
    word_type = (local_entry.get("type") or "").strip().lower()
    diff = {}
    if word_type == "nomen":
        for key in ("article", "plural"):
            local_v = _normalize_answer(local_entry.get(key, ""))
            remote_v = _normalize_answer(remote_data.get(key, ""))
            if remote_v and local_v != remote_v:
                diff[key] = {"local": local_entry.get(key, ""), "remote": remote_data.get(key, "")}
    elif word_type == "verb":
        for key in ("prateritum", "partizip2"):
            local_v = _normalize_answer(local_entry.get(key, ""))
            remote_v = _normalize_answer(remote_data.get(key, ""))
            if remote_v and local_v != remote_v:
                diff[key] = {"local": local_entry.get(key, ""), "remote": remote_data.get(key, "")}
    return diff


def _vf_apply_single(db_path, row):
    entry = get_word_by_id(db_path, row.get("word_id"))
    if not entry:
        return False
    remote = row.get("remote_payload") or {}
    updates = {}
    if row.get("word_type") == "nomen":
        for key in ("article", "plural"):
            value = _safe_text(remote.get(key)).strip()
            if value:
                updates[key] = value
    elif row.get("word_type") == "verb":
        for key in ("prateritum", "partizip2"):
            value = _safe_text(remote.get(key)).strip()
            if value:
                updates[key] = value
    if not updates:
        return False
    updated = dict(entry)
    updated.update(updates)
    return bool(update_word_fields(db_path, entry.get("id"), updated))
