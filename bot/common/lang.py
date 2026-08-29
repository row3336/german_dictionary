"""Per-user interface/dictionary language and quiz-mode helpers."""

from ..app_config import OWNER_DICT_LANG_KEY, OWNER_INTERFACE_LANG_KEY, get_db_path
from ..i18n import normalize_dict_lang, normalize_interface_lang
from ..storage import QUIZ_DE_RU as MODE_DE_RU
from ..storage import QUIZ_RU_DE as MODE_RU_DE
from ..storage import get_setting, get_user_languages


DIRECTION_BASE_MODES = (
    "ru_de", "uk_de", "en_de",
    "de_ru", "de_uk", "de_en",
)


def direction_mode_family(base_mode):
    """Return the exact word/phrase modes belonging to one direction."""
    mode = str(base_mode or "").strip()
    if mode.startswith("phrase_"):
        mode = mode[len("phrase_"):]
    if mode not in DIRECTION_BASE_MODES:
        raise ValueError(f"not a direction mode: {base_mode!r}")
    return mode, f"phrase_{mode}"


def phrase_mode(base_mode):
    """Return the exact phrase SRS mode for a supported direction."""
    return direction_mode_family(base_mode)[1]


def selected_direction_mode(context, base_mode):
    """Resolve the exact direction mode retained in this Telegram flow."""
    family = direction_mode_family(base_mode)
    selected = str(context.user_data.get("quiz_mode") or "")
    return selected if selected in family else family[0]


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


# All possible `_dict_label` outputs — navigation maps every direction variant
# back to its action, so a button routes regardless of the user's dict language
# (and survives a stale keyboard after a dict-language change). (#17)
_QUIZ_DIR_SOURCES = ("Ru", "Ua", "En")


def _quiz_dir_labels(user_id):
    """Quiz-direction labels using the user's DICTIONARY language (#17).

    Returns (L1>De, De>L1), e.g. dict=ru -> ("Ru > De", "De > Ru"). The same
    label is used to render the keyboard and to route the press back to an action.
    """
    src = _dict_label(user_id)
    return f"{src} > De", f"De > {src}"


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
