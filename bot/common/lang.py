"""Per-user interface/dictionary language and quiz-mode helpers."""

from ..app_config import OWNER_DICT_LANG_KEY, OWNER_INTERFACE_LANG_KEY, get_db_path
from ..i18n import normalize_dict_lang, normalize_interface_lang
from ..storage import QUIZ_DE_RU as MODE_DE_RU
from ..storage import QUIZ_RU_DE as MODE_RU_DE
from ..storage import get_setting, get_user_languages


def _user_interface_lang(user_id):
    """This user's interface language (per-user, from the `users` table).

    Falls back to the legacy/global owner setting when the user hasn't chosen one
    yet — so the owner's existing preference survives and it doubles as the default.
    """
    db = get_db_path()
    ui, _ = get_user_languages(db, user_id)
    if ui:
        return normalize_interface_lang(ui, default="en")
    return normalize_interface_lang(get_setting(db, OWNER_INTERFACE_LANG_KEY, "en"), default="en")


def _user_dict_lang(user_id):
    """This user's dictionary language (per-user, `users` table) with global fallback."""
    db = get_db_path()
    _, dl = get_user_languages(db, user_id)
    if dl:
        return normalize_dict_lang(dl, default="ru")
    return normalize_dict_lang(get_setting(db, OWNER_DICT_LANG_KEY, "ru"), default="ru")


def _dict_label(user_id):
    """Handle dict label."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "Ua"
    if dict_lang == "en":
        return "En"
    return "Ru"


def _dict_lang_tag(user_id):
    """Handle dict lang tag."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "UA"
    if dict_lang == "en":
        return "EN"
    return "RU"


def _mode_l1_de(user_id):
    """Handle mode l1 de."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_RU_DE
    return f"{dict_lang}_de"


def _mode_de_l1(user_id):
    """Handle mode de l1."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_DE_RU
    return f"de_{dict_lang}"


def _mode_pl_sg(user_id):
    """Handle mode pl sg."""
    return f"pl_sg_{_user_dict_lang(user_id)}"
