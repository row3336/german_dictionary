"""Per-user interface/dictionary language and quiz-mode helpers."""

from ..app_config import OWNER_DICT_LANG_KEY, OWNER_INTERFACE_LANG_KEY, get_db_path
from ..i18n import normalize_dict_lang, normalize_interface_lang
from ..storage import QUIZ_DE_RU as MODE_DE_RU
from ..storage import QUIZ_RU_DE as MODE_RU_DE
from ..storage import get_setting


def _user_interface_lang(user_id):
    """Handle user interface lang."""
    lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "en")
    return normalize_interface_lang(lang, default="en")


def _user_dict_lang(user_id):
    """Handle user dict lang."""
    dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "ru")
    return normalize_dict_lang(dict_lang, default="ru")


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
