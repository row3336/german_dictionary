LANG_EN = "en"
LANG_RU = "ru"
LANG_UK = "uk"

DICT_RU = "ru"
DICT_UK = "uk"
DICT_EN = "en"

INTERFACE_LANGS = (LANG_EN, LANG_RU, LANG_UK)
DICT_LANGS = (DICT_RU, DICT_UK, DICT_EN)


TEXTS = {
    "en": {
        "start": "Start",
        "back": "back",
        "stop": "stop",
        "add_word": "Add words",
        "tests": "Quiz",
        "settings": "Settings",
        "info": "Info",
        "stat": "Statistics",
        "db_search": "DB search",
        "db_search_matches": "search matches",
        "db_search_plural_match": "plural_match",
        "db_search_topic_words": "words_lists",
        "db_search_topics_lists": "topics_lists",
        "db_search_type_lists": "type_lists",
        "db_search_topics_lern": "topics_lern",
        "db_search_weak_words": "weak_words",
        "main_menu": "Main menu:",
        "choose_test": "Choose test mode:",
        "quiz_l1_de": "L1 > De",
        "quiz_de_l1": "De > L1",
        "learning": "Learning",
        "quiz_article": "Articles",
        "dump_db": "dump DB",
        "dump_logs": "dump logs",
        "dump": "dump",
        "suggested_words": "Suggested words",
        "review_flags": "Review Flags ⚠️",
        "auto_dump": "auto dump",
        "levels": "Levels",
        "interface_lang_btn": "interface language",
        "dict_lang_btn": "dict language",
        "daily": "Daily",
        "weekly": "Weekly",
        "off": "Off",
        "choose_ui_lang": "Choose interface language:",
        "choose_dict_lang": "Choose dictionary language:",
        "dict_ru": "DE->RU (German + Russian)",
        "dict_uk": "DE->UA (German + Ukrainian)",
        "dict_en": "DE->EN (German + English)",
        "unknown_menu": "Unknown command. Use the menu buttons.",
        "delivery_failed": "⚠️ Something went wrong delivering that — please repeat your last action.",
        "settings_title": "Settings:",
        "algo_settings": "algo settings",
        "ses_rtr_gap": "ses.rtr_gap",
        "ses_rtr_it": "ses.rtr_it",
        "reset_periodicity": "reset periodicity",
        "reset_defaults": "reset to default",
        "topic_filter": "topic filter",
        "drill_sett": "drill_sett",
        "weak_drill_gap": "weak.drill_gap",
        "weak_drill_repeats": "weak.drill_repeats",
        "learning_sett": "learning",
        "add_for_learning": "add for learning",
        "learning_start": "learning session",
        "learning_skip": "skip_learning",
        "learning_next": "next",
        "learning_reset": "reset learning",
        "send_learn": "send_learn",
        "learning_daily_qty": "learning.daily_qty",
        "learning_intervals": "learning.intervals_sec",
        "learning_reminder_interval": "learning.reminder_interval_sec",
        "learning_reminder_cooldown": "learning.reminder_cooldown_sec",
        "learning_ready_quorum": "learning.ready_quorum_pct",
        "unit_tests": "unit tests",
        "standard_fsrs": "Standart mode FSRS",
        "weak_article_mode": "Article Weak Mode",
        "weak_plural_mode": "Plural Weak Mode",
        "weak_verb_mode": "Verb Forms Weak Mode",
        "weak_prateritum_mode": "Prateritum Weak Mode",
        "weak_partizip2_mode": "Partizip2 Weak Mode",
        "learning_mix_mode": "mix_mode",
        "clarify_fields": "clarify_fields",
    },
    "ru": {
        "start": "Старт",
        "back": "Назад",
        "stop": "Стоп",
        "add_word": "Добавить слово",
        "tests": "Тесты",
        "settings": "Настройки",
        "info": "Инфо",
        "stat": "Статистика",
        "db_search": "Поиск БД",
        "db_search_matches": "поиск совпадений",
        "db_search_plural_match": "по plural",
        "db_search_topic_words": "слова по темам",
        "db_search_topics_lists": "список тем",
        "db_search_type_lists": "по типу слова",
        "db_search_topics_lern": "очередь в изучении",
        "db_search_weak_words": "слабые слова",
        "main_menu": "Главное меню:",
        "choose_test": "Выберите режим теста:",
        "quiz_l1_de": "Тест L1 -> DE",
        "quiz_de_l1": "Тест DE -> L1",
        "learning": "Изучение",
        "quiz_article": "Тест артиклей",
        "dump_db": "Дамп БД",
        "dump_logs": "Дамп логов",
        "suggested_words": "Предложенные слова",
        "review_flags": "Флаги ⚠️",
        "auto_dump": "Автодамп",
        "levels": "Уровни",
        "interface_lang_btn": "Язык интерфейса",
        "dict_lang_btn": "Язык словаря",
        "daily": "Ежедневно",
        "weekly": "Еженедельно",
        "off": "Выключено",
        "choose_ui_lang": "Выберите язык интерфейса:",
        "choose_dict_lang": "Выберите язык словаря:",
        "dict_ru": "DE->RU (немецкий + русский)",
        "dict_uk": "DE->UA (немецкий + украинский)",
        "dict_en": "DE->EN (немецкий + английский)",
        "unknown_menu": "Неизвестная команда. Используйте кнопки меню.",
        "delivery_failed": "⚠️ Сообщение не дошло — повтори последнее действие.",
        "settings_title": "Настройки:",
        "algo_settings": "настройки алгоритма",
        "ses_rtr_gap": "интервал повтора",
        "ses_rtr_it": "повторов подряд",
        "reset_periodicity": "сброс переодичности",
        "reset_defaults": "сброс настроек к дефолту",
        "topic_filter": "фильтр тем",
        "drill_sett": "настройки дрилл повторов",
        "weak_drill_gap": "дрилл_интервал",
        "weak_drill_repeats": "дрилл_повторы",
        "learning_sett": "настройки изучения",
        "add_for_learning": "добавить в изучение",
        "learning_start": "сессия изучения",
        "learning_skip": "пропустить",
        "learning_next": "далее",
        "learning_reset": "сброс сессии изучения",
        "send_learn": "отправить учить",
        "learning_daily_qty": "слов в день",
        "learning_intervals": "интервалы (сек)",
        "learning_reminder_interval": "напоминание_интервал",
        "learning_reminder_cooldown": "напоминание_пауза",
        "learning_ready_quorum": "кворум готовности %",
        "unit_tests": "unit tests",
        "standard_fsrs": "Основной (FSRS)",
        "weak_article_mode": "Слабые артикли",
        "weak_plural_mode": "Слабые plural",
        "weak_verb_mode": "Слабые формы глагола",
        "weak_prateritum_mode": "Слабые Prateritum",
        "weak_partizip2_mode": "Слабые Partizip2",
        "learning_mix_mode": "микс режим",
        "clarify_fields": "уточнить поля",
    },
    "uk": {
        "start": "Старт",
        "back": "Назад",
        "stop": "Стоп",
        "add_word": "Додати слово",
        "tests": "Тести",
        "settings": "Налаштування",
        "info": "Інфо",
        "stat": "Статистика",
        "db_search": "Пошук БД",
        "db_search_matches": "пошук збігів",
        "db_search_plural_match": "за plural",
        "db_search_topic_words": "слова за темами",
        "db_search_topics_lists": "список тем",
        "db_search_type_lists": "за типом слова",
        "db_search_topics_lern": "черга у вивченні",
        "db_search_weak_words": "слабкі слова",
        "main_menu": "Головне меню:",
        "choose_test": "Оберіть режим тесту:",
        "quiz_l1_de": "Тест L1 -> DE",
        "quiz_de_l1": "Тест DE -> L1",
        "learning": "Вивчення",
        "quiz_article": "Тест артиклів",
        "dump_db": "Дамп БД",
        "dump_logs": "Дамп логів",
        "suggested_words": "Запропоновані слова",
        "review_flags": "Прапорці ⚠️",
        "auto_dump": "Автодамп",
        "levels": "Рівні",
        "interface_lang_btn": "Мова інтерфейсу",
        "dict_lang_btn": "Мова словника",
        "daily": "Щодня",
        "weekly": "Щотижня",
        "off": "Вимкнено",
        "choose_ui_lang": "Оберіть мову інтерфейсу:",
        "choose_dict_lang": "Оберіть мову словника:",
        "dict_ru": "DE->RU (німецька + російська)",
        "dict_uk": "DE->UA (німецька + українська)",
        "dict_en": "DE->EN (німецька + англійська)",
        "unknown_menu": "Невідома команда. Використайте кнопки меню.",
        "delivery_failed": "⚠️ Повідомлення не дійшло — повтори останню дію.",
        "settings_title": "Налаштування:",
        "algo_settings": "налаштування алгоритму",
        "ses_rtr_gap": "інтервал повтору",
        "ses_rtr_it": "повторів поспіль",
        "reset_periodicity": "скидання періодичності",
        "reset_defaults": "скинути налаштування до дефолту",
        "topic_filter": "фільтр тем",
        "drill_sett": "налаштування дрилл повторів",
        "weak_drill_gap": "дрил_інтервал",
        "weak_drill_repeats": "дрил_повтори",
        "learning_sett": "налаштування вивчення",
        "add_for_learning": "додати до вивчення",
        "learning_start": "сесія вивчення",
        "learning_skip": "пропустити",
        "learning_next": "далі",
        "learning_reset": "скидання сесіі вивчення",
        "send_learn": "відправити вчити",
        "learning_daily_qty": "слів на день",
        "learning_intervals": "інтервали (сек)",
        "learning_reminder_interval": "нагадування_інтервал",
        "learning_reminder_cooldown": "нагадування_пауза",
        "learning_ready_quorum": "кворум готовності %",
        "unit_tests": "unit tests",
        "standard_fsrs": "Основний (FSRS)",
        "weak_article_mode": "Слабкі артиклі",
        "weak_plural_mode": "Слабкі plural",
        "weak_verb_mode": "Слабкі форми дієслова",
        "weak_prateritum_mode": "Слабкі Prateritum",
        "weak_partizip2_mode": "Слабкі Partizip2",
        "learning_mix_mode": "мікс режим",
        "clarify_fields": "уточнити поля",
    },
}


def normalize_interface_lang(value, default="en"):
    token = str(value or "").strip().lower()
    return token if token in INTERFACE_LANGS else default


def normalize_dict_lang(value, default="ru"):
    token = str(value or "").strip().lower()
    return token if token in DICT_LANGS else default


def t(lang, key):
    lang = normalize_interface_lang(lang)
    return TEXTS.get(lang, TEXTS["en"]).get(key, TEXTS["en"].get(key, key))


def interface_choices():
    return ["English 🇬🇧", "Русский", "Українська 🇺🇦"]


def parse_interface_choice(text):
    token = str(text or "").strip()
    if token == "English 🇬🇧":
        return "en"
    if token == "Русский":
        return "ru"
    if token == "Українська 🇺🇦":
        return "uk"
    return ""


def dict_choices(lang):
    return [t(lang, "dict_ru"), t(lang, "dict_uk"), t(lang, "dict_en")]


def parse_dict_choice(text, lang):
    token = str(text or "").strip()
    if token == t(lang, "dict_ru"):
        return "ru"
    if token == t(lang, "dict_uk"):
        return "uk"
    if token == t(lang, "dict_en"):
        return "en"
    return ""
