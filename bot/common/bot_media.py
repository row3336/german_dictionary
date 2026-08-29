"""Telegram-only effective image/example view for shared word entries."""

from ..app_config import is_owner
from ..storage import _DICT_TRANSLATION_COLUMN, get_user_languages, get_user_word_media


def resolve_bot_word_media(db_path, user_id, shared_entry):
    """Return a new Telegram view of a shared entry with effective personal media.

    Storage's shared overlay deliberately hides shared media from guests for the
    Mini App contract. Telegram instead falls back independently per media field.
    The caller must pass the unoverlaid shared entry.
    """
    out = dict(shared_entry or {})
    if not user_id or not out.get("id") or is_owner(user_id):
        return out

    media = get_user_word_media(db_path, user_id, out["id"])
    for field in ("image", "example"):
        personal = media.get(field, "")
        if str(personal or "").strip():
            out[field] = personal

    _, dict_lang = get_user_languages(db_path, user_id)
    translation_column = _DICT_TRANSLATION_COLUMN.get(dict_lang)
    out["my_translation"] = media.get(translation_column, "") if translation_column else ""
    return out
