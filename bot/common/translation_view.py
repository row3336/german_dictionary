"""Strict language selection for ordinary dictionary and clarify cards."""

TRANSLATION_COLUMNS = {"ru": "russian", "uk": "ukrainian", "en": "english"}


def shared_translation(entry, dict_lang):
    """An empty selected column stays empty; another language is never substituted."""
    return str((entry or {}).get(TRANSLATION_COLUMNS.get(dict_lang), "") or "").strip()


def effective_translation(entry, dict_lang):
    """The Telegram resolver limits my_translation to this user's language."""
    if dict_lang not in TRANSLATION_COLUMNS:
        return ""
    return str((entry or {}).get("my_translation", "") or "").strip() or shared_translation(entry, dict_lang)


def translation_card_lines(entry, dict_lang, owner=False):
    columns = TRANSLATION_COLUMNS.values() if owner else (TRANSLATION_COLUMNS.get(dict_lang),)
    return [(column, str(entry.get(column, "") or "").strip() or "-") for column in columns if column]
