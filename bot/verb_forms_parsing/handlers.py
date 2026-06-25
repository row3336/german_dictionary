"""verbformen parsing UI: menu keyboard, menu sender, background dry-run task.

The parse_VF action routing lives in the Settings handler (settings_menu_choice);
this module provides the menu + the long-running dry-run task it starts.
"""

import asyncio
import random

from telegram import ReplyKeyboardMarkup

from ..app_config import SETTINGS_MENU, BACK_TEXT, get_db_path
from ..labels import (
    VF_APPLY_TEXT,
    VF_APPROVE_TEXT,
    VF_DRY_RUN_ALL_TEXT,
    VF_DRY_RUN_NEW_TEXT,
    VF_RETRY_TEXT,
    VF_SET_JITTER_TEXT,
    VF_SET_PROXY_TEXT,
    VF_SET_SLEEP_TEXT,
    VF_SHOW_DIFF_TEXT,
    VF_STOP_TEXT,
    VF_TEST_PROXY_TEXT,
    VF_VERIFY_TEXT,
)
from ..storage import _safe_text, save_vf_check_result
from .parser import (
    _mask_proxy,
    _vf_compare,
    _vf_fetch_word,
    _vf_parse_noun,
    _vf_parse_verb,
    _vf_settings_snapshot,
    _vf_user_agent,
)

# Shared runtime state (task handles + per-user stop flags), referenced by the
# Settings handler that starts/stops parsing.
VF_RUNNING_TASKS = {}
VF_STOP_FLAGS = {}


def _vf_menu_markup():
    """Build compact VF menu keyboard."""
    items = [
        VF_SET_PROXY_TEXT,
        VF_TEST_PROXY_TEXT,
        VF_SET_SLEEP_TEXT,
        VF_SET_JITTER_TEXT,
        VF_DRY_RUN_NEW_TEXT,
        VF_DRY_RUN_ALL_TEXT,
        VF_SHOW_DIFF_TEXT,
        VF_APPROVE_TEXT,
        VF_APPLY_TEXT,
        VF_VERIFY_TEXT,
        VF_RETRY_TEXT,
        VF_STOP_TEXT,
    ]
    rows = [items[i:i + 4] for i in range(0, len(items), 4)]
    if rows and len(rows[-1]) < 4:
        rows[-1].append(BACK_TEXT)
    else:
        rows.append([BACK_TEXT])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _vf_run_dry_task(bot, chat_id, user_id, candidates, sleep_sec, jitter_pct, proxy, label):
    """Run VF dry check in background with progress and stop support."""
    db_path = get_db_path()
    total = len(candidates)
    nouns = sum(1 for x in candidates if (x.get("type") or "").strip().lower() == "nomen")
    verbs = sum(1 for x in candidates if (x.get("type") or "").strip().lower() == "verb")
    await bot.send_message(
        chat_id=chat_id,
        text=f"VF parse started ({label}). total={total}, nomen={nouns}, verb={verbs}",
    )
    checked = 0
    mismatch = 0
    match = 0
    not_found = 0
    errors = 0
    ban_streak = 0
    for item in candidates:
        if VF_STOP_FLAGS.get(int(user_id)):
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"VF parse stopped by user. checked={checked}/{total}, match={match}, "
                    f"mismatch={mismatch}, not_found={not_found}, fetch_error={errors}"
                ),
            )
            return
        word_type = item.get("type")
        base = item.get("infinitive") if word_type == "verb" else item.get("german")
        base = _safe_text(base).strip()
        if not base:
            continue
        local_payload = {}
        if word_type == "nomen":
            local_payload = {"article": item.get("article", ""), "plural": item.get("plural", "")}
        elif word_type == "verb":
            local_payload = {"prateritum": item.get("prateritum", ""), "partizip2": item.get("partizip2", "")}
        try:
            html = await asyncio.to_thread(_vf_fetch_word, base, proxy, _vf_user_agent(), 30)
            if "verbformen" not in html.lower():
                raise RuntimeError("unexpected response")
            remote = _vf_parse_noun(html) if word_type == "nomen" else _vf_parse_verb(html)
            if not any(_safe_text(v).strip() for v in remote.values()):
                status = "not_found"
                not_found += 1
                diff = {}
            else:
                diff = _vf_compare(item, remote)
                status = "mismatch" if diff else "match"
                mismatch += int(bool(diff))
                match += int(not diff)
            ban_streak = 0
            save_vf_check_result(
                db_path,
                user_id,
                item.get("id"),
                word_type,
                base,
                status,
                retryable=False,
                local_payload=local_payload,
                remote_payload=remote,
                diff_payload=diff,
                source_hash=item.get("vf_source_hash", ""),
            )
            checked += 1
        except Exception as exc:
            errors += 1
            checked += 1
            ban_streak += 1
            save_vf_check_result(
                db_path,
                user_id,
                item.get("id"),
                word_type,
                base,
                "fetch_error",
                retryable=True,
                local_payload=local_payload,
                remote_payload={"error": str(exc)},
                diff_payload={},
                source_hash=item.get("vf_source_hash", ""),
            )

        if checked % 5 == 0 or checked == total:
            progress = int((checked * 100) / total) if total else 100
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"VF progress: {checked}/{total} ({progress}%) | "
                    f"match={match}, mismatch={mismatch}, not_found={not_found}, fetch_error={errors}"
                ),
            )

        if ban_streak >= 10:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "Emergency stop: 10 consecutive fetch errors. "
                    "Possible ban/rate-limit. Stop parsing now and adjust proxy/sleep."
                ),
            )
            return

        jitter = max(0, int(sleep_sec * jitter_pct / 100))
        delay = max(1, sleep_sec + random.randint(-jitter, jitter))
        await asyncio.sleep(delay)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"VF parse done. checked={checked}/{total}, match={match}, "
            f"mismatch={mismatch}, not_found={not_found}, fetch_error={errors}"
        ),
    )


async def _send_vf_menu(update, prefix_text=None):
    proxy, sleep_sec, jitter_pct = _vf_settings_snapshot()
    lines = [
        "parse_VF:",
        f"proxy = {_mask_proxy(proxy) or '-'}",
        f"sleep = {sleep_sec}s",
        f"jitter = {jitter_pct}%",
        "",
        "approve format: approve 12,19,25",
    ]
    text = "\n".join(lines)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(text, reply_markup=_vf_menu_markup())
    return SETTINGS_MENU
