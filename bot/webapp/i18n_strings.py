"""UI strings for the Mini App, served by /me (miniapp.md §7.1).

The frontend carries no human-readable string of its own: it renders whatever
`strings` the server sends, so a translation is fixed in one place and three
languages stay in step.

Why these live here and not in bot/i18n.py:

  * the bot's catalogue is written for a chat with reply keyboards — several of
    its texts name buttons the app does not have ("press Cancel"), so reusing
    them verbatim would describe an interface the user is not looking at;
  * this branch deliberately does not edit bot/i18n.py, the file feature/multiuser
    also touches (see the branch note in claude_docs/current.md).

German grammar terms are NOT here on purpose (§8.4a): `Präsens`, `pl. die Tische`,
`+Dativ`, `artikel Singular Plural` and friends stay German for every interface
language — we are teaching German, not translating it. Only the translation of a
word, block headers and interface controls are localized.
"""

from ..i18n import normalize_interface_lang

# English is the fallback layer: any key missing from ru/uk falls back to it, the
# same rule bot/i18n.py:t() applies.
APP_TEXTS = {
    "en": {
        # main screen
        "app_title": "Deutsch",
        "mode_l1_de": "To German",
        "mode_de_l1": "From German",
        "mode_pl_sg": "Plural → Singular",
        "mode_weak_article": "Article",
        "mode_weak_plural": "Plural",
        "mode_weak_prateritum": "Präteritum",
        "mode_weak_partizip2": "Partizip II",
        "mode_learning": "Learning",
        "due_none": "nothing due",
        "tab_dictionary": "Dictionary",
        "topics_all": "all topics",
        "topics_apply": "Apply",
        "topics_select_all": "Select all",
        "words_count": "words",
        # quiz
        "answer_placeholder": "your answer",
        "action_idk": "I don't know",
        "action_input_err": "typo",
        "action_send_learn": "to learning",
        "action_next": "next",
        "action_stop": "finish",
        "verdict_correct": "Correct",
        "verdict_wrong": "Wrong",
        "expected_answer": "Expected",
        "is_retry": "repeat",
        # session summary
        "summary_title": "Session finished",
        "summary_answered": "answered",
        "summary_correct": "correct",
        "summary_wrong": "wrong",
        "summary_retries": "repeats",
        # learning
        "learning_next": "next",
        "learning_skip": "skip",
        "learning_add_batch": "Add words",
        "learning_batch_done": "Batch finished",
        # dictionary and word card
        "search_placeholder": "German or translation",
        "search_empty": "Nothing found",
        "search_choose": "Which one?",
        "card_synonyms": "Synonyms",
        "card_example": "Example",
        "card_image": "Image",
        "card_study": "Study status",
        "card_synonym_of": "synonym of",
        # states
        "state_no_due": "Nothing to repeat right now",
        "state_taken_over": "The session continued in the chat",
        "state_continue_here": "Continue here",
        "state_offline": "No connection",
        "state_retry": "Try again",
        "state_chat_only": "This is done in the chat",
        # Sent into the chat, not rendered in the app: the one message that explains
        # why the question hanging in the conversation is no longer live (§4.2).
        "chat_session_moved": "▶️ The session continues in the app. The question above is no longer active.",
        # errors (§7.7)
        "error_generic": "Something went wrong",
        "error_reopen": "Please open the app again",
        "error_rate_limited": "Too fast, wait a moment",
        "access_pending": "Your request is with the owner. The answer will come in the chat.",
        "access_rejected": "Access was not granted.",
        "access_unknown": "Press /start in the bot first.",
    },
    "ru": {
        "app_title": "Deutsch",
        "mode_l1_de": "На немецкий",
        "mode_de_l1": "С немецкого",
        "mode_pl_sg": "Мн. → ед.",
        "mode_weak_article": "Артикль",
        "mode_weak_plural": "Множественное",
        "mode_weak_prateritum": "Präteritum",
        "mode_weak_partizip2": "Partizip II",
        "mode_learning": "Изучение",
        "due_none": "нечего повторять",
        "tab_dictionary": "Словарь",
        "topics_all": "все темы",
        "topics_apply": "Применить",
        "topics_select_all": "Выбрать все",
        "words_count": "слов",
        "answer_placeholder": "ваш ответ",
        "action_idk": "не знаю",
        "action_input_err": "опечатка",
        "action_send_learn": "в изучение",
        "action_next": "дальше",
        "action_stop": "завершить",
        "verdict_correct": "Верно",
        "verdict_wrong": "Неверно",
        "expected_answer": "Ожидалось",
        "is_retry": "повтор",
        "summary_title": "Сессия завершена",
        "summary_answered": "отвечено",
        "summary_correct": "верно",
        "summary_wrong": "неверно",
        "summary_retries": "повторов",
        "learning_next": "дальше",
        "learning_skip": "пропустить",
        "learning_add_batch": "Добрать слова",
        "learning_batch_done": "Партия закрыта",
        "search_placeholder": "немецкое слово или перевод",
        "search_empty": "Ничего не найдено",
        "search_choose": "Какое из них?",
        "card_synonyms": "Синонимы",
        "card_example": "Пример",
        "card_image": "Образ",
        "card_study": "Статус изучения",
        "card_synonym_of": "синоним к",
        "state_no_due": "Сейчас нечего повторять",
        "state_taken_over": "Сессия продолжена в чате",
        "state_continue_here": "Продолжить здесь",
        "state_offline": "Нет связи",
        "state_retry": "Повторить",
        "state_chat_only": "Это делается в чате",
        "chat_session_moved": "▶️ Сессия продолжается в приложении. Вопрос выше больше не активен.",
        "error_generic": "Что-то сломалось",
        "error_reopen": "Откройте приложение заново",
        "error_rate_limited": "Слишком часто, подождите",
        "access_pending": "Заявка у владельца. Ответ придёт в чат.",
        "access_rejected": "Доступ не предоставлен.",
        "access_unknown": "Сначала нажмите /start в боте.",
    },
    "uk": {
        "app_title": "Deutsch",
        "mode_l1_de": "На німецьку",
        "mode_de_l1": "З німецької",
        "mode_pl_sg": "Мн. → одн.",
        "mode_weak_article": "Артикль",
        "mode_weak_plural": "Множина",
        "mode_weak_prateritum": "Präteritum",
        "mode_weak_partizip2": "Partizip II",
        "mode_learning": "Вивчення",
        "due_none": "немає що повторювати",
        "tab_dictionary": "Словник",
        "topics_all": "усі теми",
        "topics_apply": "Застосувати",
        "topics_select_all": "Обрати всі",
        "words_count": "слів",
        "answer_placeholder": "ваша відповідь",
        "action_idk": "не знаю",
        "action_input_err": "друкарська помилка",
        "action_send_learn": "у вивчення",
        "action_next": "далі",
        "action_stop": "завершити",
        "verdict_correct": "Правильно",
        "verdict_wrong": "Неправильно",
        "expected_answer": "Очікувалось",
        "is_retry": "повтор",
        "summary_title": "Сесію завершено",
        "summary_answered": "відповідей",
        "summary_correct": "правильних",
        "summary_wrong": "неправильних",
        "summary_retries": "повторів",
        "learning_next": "далі",
        "learning_skip": "пропустити",
        "learning_add_batch": "Додати слова",
        "learning_batch_done": "Партію закрито",
        "search_placeholder": "німецьке слово або переклад",
        "search_empty": "Нічого не знайдено",
        "search_choose": "Яке саме?",
        "card_synonyms": "Синоніми",
        "card_example": "Приклад",
        "card_image": "Образ",
        "card_study": "Статус вивчення",
        "card_synonym_of": "синонім до",
        "state_no_due": "Зараз немає що повторювати",
        "state_taken_over": "Сесію продовжено в чаті",
        "state_continue_here": "Продовжити тут",
        "state_offline": "Немає зв'язку",
        "state_retry": "Спробувати ще",
        "state_chat_only": "Це робиться в чаті",
        "chat_session_moved": "▶️ Сесія продовжується в застосунку. Питання вище більше не активне.",
        "error_generic": "Щось зламалось",
        "error_reopen": "Відкрийте застосунок заново",
        "error_rate_limited": "Занадто часто, зачекайте",
        "access_pending": "Заявка у власника. Відповідь прийде в чат.",
        "access_rejected": "Доступ не надано.",
        "access_unknown": "Спершу натисніть /start у боті.",
    },
}


def app_strings(lang):
    """Full string table for one interface language, English-filled."""
    lang = normalize_interface_lang(lang)
    strings = dict(APP_TEXTS["en"])
    strings.update(APP_TEXTS.get(lang, {}))
    return strings
