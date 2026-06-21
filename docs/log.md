# Change Log

> Лог правок проекта для быстрого восстановления истории. Новые записи — **сверху**.
> Формат записи:
> ```
> ## YYYY-MM-DD — краткий заголовок
> - **commit:** <hash или «локально/без коммита»>
> - **файлы:** список
> - **что:** суть изменения
> - **почему:** причина
> - **проверка:** как проверяли
> ```

## 2026-06-21 — AI-парсинг add_words: детерминизм + JSON mode + few-shot (ветка)
- **commit:** `516baaa`
- **файлы:** `bot/ai.py`, `bot/handlers/add_words.py`
- **что:** снижение *устранимого* вранья nano на артиклях/плюралях/формах глаголов без смены модели. (1) `temperature=0` в `generate_fields` — детерминированное декодирование для извлечения фактов (убирает случайное враньё «проигранный кубик» + воспроизводимость). Защита `_supports_temperature()`: reasoning-модели (`o1/o3/o4/gpt-5*`) отвергают `temperature≠1` → им параметр не шлётся. (2) `response_format={"type":"json_object"}` — гарантированно валидный JSON (промпты уже содержат «JSON»), меньше срывов парсинга. (3) few-shot в `_ai_prompt`: 4 примера с нерегулярными плюралями (`Wort→Wörter`, `Bus→Busse`, `Stadt→Städte`) + verb-пример, инструкция «плюрали нерегулярны, не достраивать по -e/-s».
- **почему:** артикль/плюраль — фактическая память, не логика; temperature=1.0 (дефолт) добавлял рандом в задачу, где он вреден. Модель крупнее снижает ошибку частично и за деньги — бесплатные рычаги выжали сначала. Гарантию точности по-прежнему даёт verbformen (parse_VF, не трогали).
- **важно:** НЕ выключатель галлюцинаций — если nano уверенно не знает форму, она останется неверной (теперь стабильно). Источник истины = verbformen.
- **проверка:** мок-вызов подтвердил kwargs (nano → `temperature=0`+json_object; gpt-5 → без temperature, json_object есть); few-shot в промпте; `import bot.main` OK; 49/49 тестов.

## 2026-06-21 — Сетевые фиксы квизов: error-handler + retry-wrap + таймауты (ветка)
- **commit:** `e4a5fae`
- **файлы:** `bot/main.py`, `bot/i18n.py`, `bot/handlers/quiz.py`, `bot/handlers/learning.py`, `docs/todo.md`
- **что:** 3 точечных фикса по диагнозу задержек/потерь квизов (причина — сеть VPS↔Telegram, доказана логами). (1) `app.add_error_handler(_on_error)` — лог трейсбека + одно уведомление `delivery_failed` (i18n en/ru/uk, по языку владельца), БЕЗ авто-переотправки (дубль не плодит); ошибки long-poll/get_updates сюда не попадают → нет шторма уведомлений. (2) Добиты 3 оставшихся raw `reply_text` (`quiz.py:245`, `learning.py:248/255`) → `_reply_text_with_retry`. (3) Таймауты/пул в `Application.builder()`: `connect/read/write=20с` (было 5), `pool_timeout=10` (было 1), `connection_pool_size=8` (было 1) — дефолты тесны для дёрганого роута.
- **почему:** в логах прямым текстом `No error handlers are registered`; дефолт connect 5с + пул=1 → ConnectTimeout/исчерпание пула → потерянные/задержанные отправки квизов.
- **остаток:** риск дубля ВНУТРИ `_reply_text_with_retry` (sendMessage доставлен, HTTP-ответ потерян → ретрай шлёт дубль) — редкий, вынесен в todo. Эксплуатация: при редеплое убивать старый процесс (иначе `Conflict`).
- **проверка:** все 5 сеттеров билдера + `add_error_handler` существуют в PTB 20.7; `import bot.main` OK; 49/49 тестов.

## 2026-06-16 — Рефакторинг main.py: Этап 2 (ветка refactor/split-main-stage1)
- **commit:** `bc73b75` (на ветке)
- **файлы:** `bot/common/session.py` (new), `bot/main.py`
- **что:** вынесена внутрисессионная механика очередей: session-tracking, FSRS cycle queue (`_pick_due_entry`, `_reset_sm2_cycle_state`), session-retry glue, weak-drill glue. Фиче-специфичное (`_pick_weak_entry`, weak-answer-хелперы) осталось в main. Поведение 1:1, ключи `context.user_data` не менялись.
- **проверка:** `import bot.main` OK, рантайм-harness (session/retry/weak-drill + `_pick_due_entry` на копии БД) OK, 38 тестов OK. main.py 6535 → 6305.

## 2026-06-16 — Тесты на непокрытые блоки + ин-бот раннер на discover (ветка)
- **commit:** `48dff60` (на ветке)
- **файлы:** `tests/test_word_casing.py` (new), `tests/test_select_due_gating.py` (new), `bot/main.py`
- **что:** покрыты непокрытые блоки: нормализация казинга (`_normalize_word_entry_for_storage` — зона `_cap_word`), `select_due` gating (introduced-gate, progress-alone, `exclude_correct_today`, isolation по user/mode = защита от бага порядка параметров, forecast↔очередь). `_run_unit_tests_report` переключён на `unittest discover` → кнопка `unit_tests` в боте подхватывает новые тесты автоматически. Suite 38 → **49** тестов, все зелёные.
- **почему:** покрыть тестами перед SM2→FSRS переименованием (чтобы рефакторинг был под защитой).

## 2026-06-19 — Багфиксы (ветка): De→Ru, send_learn, бэкапы, напоминание
- **commits:** `bf18c29` (De→Ru example/image + чистка бэкапов БД), `34acc6a` (send_learn upsert), `9bda53c` (FSRS-напоминание 18:00)
- **De→Ru (`bf18c29`):** `_format_feedback_de_ru` — убрана word card (в квизе её быть не должно), вместо неё `_append_hint_lines` (example+image, только на неверный).
- **Бэкапы (`bf18c29`):** `_cleanup_old_dump_logs` теперь удаляет и `words-*.db` старше 14 дней (не только логи).
- **send_learn (`34acc6a`):** `learning_add_word_by_id` → upsert (ON CONFLICT DO UPDATE): слово всегда (пере)добавляется как новое (шаг 0, preview заново). Сообщение «✅ Added to learning», «Already in learning» удалено. Проверено харнессом (completed → re-add → active/шаг0).
- **Напоминания (`9bda53c`→`7254d25`):** ДВА независимых джоба в `jobs/reminders.py`. (1) **learning** (батч, quorum/interval/cooldown) — сохранён дословно. (2) **FSRS** в 18:00 Europe/Berlin (DST-aware), 1×/день, если `fsrs_due_forecast.total>0` — новый отдельный джоб (`FSRS_AFTERNOON_REMINDER_JOB_NAME`). `_configure_learning_reminders` планирует оба. Утренний FSRS-счётчик + retention — в `jobs/reports.py` (не трогал). Примечание: в 9bda53c learning ошибочно заменили, в 7254d25 восстановлен. Проверено харнессом (FSRS: до 18 тихо / due=0 тихо / due>0 шлёт / повтор не дублирует).
- **проверка:** 49 тестов на каждом коммите.

## 2026-06-18 — Diff-аудит рефакторинга (ветка) — ПРОЙДЕН
- **что:** пофункциональное сравнение тел всех 272 функций исходного main:bot/main.py с версиями на ветке (AST get_source_segment, нормализация отступов/пустых строк).
- **итог:** 225 идентичны байт-в-байт; 27 изменены + 20 отсутствуют — ВСЕ объяснимы: (1) переименование sm2→fsrs (14ab0df), (2) edit→clarify_fields (0250446/4813392), (3) добавленные отложенные импорты при разрезе, (4) переписанный _run_unit_tests_report (446511a), (5) 4 мёртвые функции удалены в слиме.
- **особо:** _safe_text — в монолите локальный `str(value or "")`, теперь storage-версия (None→""/str(value)). Расхождение только на 0/False; все реальные вызовы (db_search/vf) передают строки/None → результат идентичен. Поведение не меняется.
- **вердикт:** рефакторинг behavior-preserving, непреднамеренных изменений нет. Осталось: общий смоук в Telegram + мерж.

## 2026-06-18 — Этап 4: финальный слим main (ветка)
- **commit:** `6580835`
- **что:** убраны ~280 неиспользуемых импортов (vf, ui.formatting, quiz/learning-внутренности, storage-хелперы) + 4 мёртвых хелпера (_parse_level_toggle, _safe_text [живой в storage], _get_ru_de_answer, _append_suggested_words) + get_suggestions_path. flags РЕШЕНО оставить в main (мультиюзер на будущее).
- **как:** AST-скрипт переписал import-стейтменты (оставил только используемые). Единственное имя, что реэкспортится из main — _flag_current_word (def, не импорт), поэтому culling безопасен.
- **проверка:** все 11 модулей import без цикла, _flag_current_word реэкспорт ок, 0 неиспользуемых импортов, 49 тестов. **main.py 788 → 422.**
- **ИТОГ: main.py 6811 → 422.** Осталось: diff-аудит main..ветка + общий смоук + мерж.

## 2026-06-18 — Этап 4: вынесен menu (ветка)
- **commit:** `77b791a` (`bot/handlers/menu.py`, 294 строки)
- **что:** меню-роутеры верхнего уровня: /start (start), главное меню (menu_choice), меню тестов (tests_menu_choice), Ru>De подменю (quiz_ru_de_menu_choice) + загрузчик info-текста (_load_info_text).
- **как:** menu — вершина дерева зависимостей (никто не импортит handlers.menu), поэтому кросс-вызовы фич (settings/db_search/quiz/learning/weak) — обычные модульные импорты, без цикла. _is_owner_only=алиас is_owner.
- **проверка:** import без цикла, кросс-импорты, 49 тестов. main.py 1038 → 788 (−250).
- **Следующий:** flags (возможно удалить по todo) → финальный слим main + diff-аудит. **Смоук:** /start, главное меню (add/tests/settings/db_search/info/stat), меню тестов, ru_de-подменю.

## 2026-06-18 — Этап 4: вынесен settings (ветка, 2 субтаска)
- **commits:** `cb1a490` (Субтаск A: подменю algo/drill/learning + хендлеры, выбор языка select_ui/dict_lang + _ask_*, _run_unit_tests_report, topic_filter_edit), `8cb509b` (Субтаск B: settings_menu_choice — vf-роутер)
- **что:** bot/handlers/settings.py (794 строки) — все настройки. A — подменю + правка значений (algo_settings_edit/confirm), выбор языка интерфейса/словаря, in-bot прогон тестов, редактор topic-filter. B — корневой роутер настроек (тяжёлый по verbformen: proxy/sleep/jitter, test, dry-run/retry/stop, show-diff/approve/apply/verify, dump, suggested_words).
- **как:** settings — высокоуровневый потребитель: импортит vf (verb_forms_parsing), dump (handlers.dump), common.dumps, add_words(отложенно для _process_next_ai_word в topic_filter_edit) — всё однонаправленно → без цикла. _is_owner_only = алиас is_owner. _configure_logging оставлен в main (bootstrap).
- **проверка:** import без цикла, орфанов нет, 49 тестов. main.py 1708 → 1038 (−670).
- **Следующий:** menu (start/menu_choice/tests_menu_choice). **Смоук:** настройки algo/drill/learning + правка, выбор языка, topic-filter, vf-меню целиком, suggested_words.

## 2026-06-18 — Этап 4: вынесен add_words (ветка, 2 субтаска)
- **commits:** `3e20010` (Субтаск A: _add_keys/_add_input_markup → ui/keyboards.py), `e80c66e` (Субтаск B: bot/handlers/add_words.py)
- **что:** весь add + AI-распознавание (31 функция, ~1071 строка): ввод текст/документ/JSON (add_input, add_document), precheck (add_precheck/add_precheck_edit + хелперы), очередь AI по словам (_process_ai_word/_process_next_ai_word/_ai_prompt/нормализация/валидация), preview/confirm/clarify (_format_ai_preview/_show_ai_preview/add_confirm/add_clarify), сохранение/бан/stop (_save_entry/_ban_pending_word/_stop_queue), add-клавиатуры (_stop_keys/_clarify_keys).
- **как:** add_words — листовой хендлер (без импорта других хендлеров → без цикла). clarify_fields отложенно берёт _show_ai_preview/_stop_queue из add_words. main держит 6 хендлеров + _process_next_ai_word (для topic_filter_edit). Удаление из main — скриптом by-name + breadcrumb.
- **проверка:** import без цикла, no orphan refs, спот-чеки парсинга/префильтра, 49 тестов. main.py 2777 → 1708 (−1069).
- **Следующий:** settings (вкл. learning-settings). **Смоук:** add — одиночное/список/.txt/JSON, precheck approve/reject/edit, confirm/reject/ban/clarify-AI/clarify-field, очередь, stop.

## 2026-06-18 — Этап 4: вынесен learning (ветка)
- **commit:** `eb1d50e` (`bot/handlers/learning.py`)
- **что:** learning меню/превью/квиз (_send_learning_menu, _send_learning_prompt, learning_menu_choice, learning_quiz), topic-mix очередь (_build_learning_topic_mix_queue, _pick_learning_due_entry), пополнение батча (_learning_add_words), input-err (_apply_input_err_learning), _format_learning_preview_line, _format_eta_from_iso, _send_entry_to_learning. learning импортирует quiz модульно (_check_ru_de_strict; quiz откладывает кросс-вызовы → без цикла); _start_clarify_fields отложенно. quiz (3 хендлера) + clarify_fields теперь берут _send_entry_to_learning / _send_learning_prompt из handlers.learning.
- **важно:** learning-НАСТРОЙКИ (_send_learning_settings_menu + learning_settings_menu_choice, состояние LEARNING_SETTINGS_MENU) намеренно ОСТАВЛЕНЫ в main — уйдут с модулем settings.
- **проверка:** import без цикла, кросс-импорты + отложенные цели резолвятся, 49 тестов. main.py 3151 → 2782 (−369).
- **Следующий:** add_words. **Смоук:** learning — меню, add words, превью next/skip, ответ/IDK/input-err, mix-mode toggle, reset, clarify из learning, fallback «no due».

## 2026-06-18 — Этап 4: вынесен weak (ветка)
- **commit:** `e44244e` (`bot/handlers/weak.py`)
- **что:** verb-weak меню (quiz_verb_weak_menu_choice), 4 weak-хендлера (article/plural/prateritum/partizip2), 4 _send_*_weak_prompt, _pick_weak_entry/_pick_weak_or_drill_entry, _remember_last_weak_answer/_apply_input_err_weak. weak импортирует quiz модульно (у quiz нет weak-ссылок → без цикла); _start_clarify_fields + _flag_current_word отложенно. clarify_fields (оба отложенных блока) и synonyms теперь берут weak-промпты из handlers.weak.
- **проверка:** import без цикла, кросс-импорты + отложенные цели резолвятся, 49 тестов. main.py 3744 → 3151 (−593).
- **Следующий:** learning. **Смоук:** weak — article/plural, verb-weak меню → präteritum/partizip2, input-err, flag, clarify из weak, fallback «no weak words».

## 2026-06-18 — Этап 3 закрыт: вынесен synonyms (ветка)
- **commit:** `686a0d9` (`bot/handlers/synonyms.py`)
- **что:** меню синонимов (markups, _syn_auto_text, _format_syn_group, _open_synonyms_menu), _return_from_synonyms (диспатч назад в quiz/db_search/weak промпты — один отложенный блок), 5 хендлеров состояний (synonyms_menu/add_input/remove_input/auto_confirm/clear_confirm). clarify_fields теперь отложенно импортирует _open_synonyms_menu из synonyms.
- **проверка:** import без цикла, кросс-импорты, 49 тестов. main.py 3744 → 3536. **Этап 3 завершён** — все фичи вынесены.
- **Следующий:** Этап 4 — weak. **Смоук:** synonyms — открыть из clarify root, add/remove/auto/clear/flag/back, возврат в каждый вызвавший режим.

## 2026-06-18 — Этап 4: вынесен db_search (ветка)
- **commit:** `de97b2e` (prep: `_split_text_chunks`→text_utils, `_format_topic_learning_line`→ui/formatting), `87a69a7` (`bot/handlers/db_search.py`)
- **что:** db_search_input (большой роутер), db_search_view, _send_db_search_prompt, markups, форматтеры, helpers. db_search↔clarify_fields: db_search импортирует clarify_fields_input (модульно), clarify_fields отложенно импортирует _send_db_search_prompt из db_search — без цикла.
- **проверка:** import без цикла, кросс-импорты резолвятся, 49 тестов. main.py 4254 → 3744.
- **Следующий:** synonyms (закроет Этап 3). **Смоук:** db_search — поиск слова, списки (matches/plural/verbs/topics/weak/learning queue), clarify из карточки.

## 2026-06-17 — Этап 4: вынесен clarify_fields (бывш. edit) + rename (ветка)
- **commit:** `0250446` (вынос, имена сохранены), `4813392` (rename → clarify_fields)
- **что:** `bot/handlers/clarify_fields.py` — `_start_clarify_fields`, `clarify_fields_input` (бывш. `add_edit`, FSM-состояние `ADD_EDIT` сохранено), `_resume_after_clarify`, `_get_clarify_target`, `_allowed_clarify_fields`, `_set_entry_field`. Клавиатуры: `_clarify_fields_markup`, `_clarify_root_markup`. Много отложенных импортов из main (weak/learning/db_search/synonyms/add prompts) + модульный импорт quiz-prompts. quiz.py отложенный импорт обновлён на clarify_fields.
- **проверка:** импорт без цикла, все 10 целей deferred на месте, 0 старых имён после rename, ADD_EDIT не тронут, 49 тестов. main.py 4612 → 4254.
- **разблокирует** db_search + synonyms. **Смоук:** clarify_fields из квиза / db_search / add-флоу — выбор поля, правка, сохранение, delete, синонимы, back.

## 2026-06-17 — Этап 4: вынесен handlers/quiz.py (quiz-домино) (ветка)
- **commit:** `c322102`
- **что:** `bot/handlers/quiz.py` — хендлеры `quiz_ru_de/de_ru/pl_sg/article`, prompt-сендеры, `_check_ru_de_strict` (+split/synonym/plural), `_update_srs_for_entry`/`_interval_divisor_for_mode`, `_apply_input_err`, `_remember_last_answer`, `_repeat_current_*`, `_stop_with_summary`. Кросс-вызовы `_send_entry_to_learning`/`_flag_current_word`/`_start_quiz_edit` (в main) — **локальные импорты внутри хендлеров** (отложенные, без цикла). `MODE_*` = строки storage, `QUIZ_*` = инты app_config — импортированы раздельно.
- **проверка:** компиляция, `import bot.main` без цикла, ре-экспорт, отложенные импорты резолвятся, sync-хелперы (делители 1.65/2.0, check nomen/verb), 49 тестов. main.py 5115 → 4612.
- **разблокирует** synonyms/db_search. **Смоук-тест в боте:** квизы ru_de/de_ru/pl_sg/article — вопрос/ответ/фидбэк, retry, input_err, send_learn, clarify_fields, back/stop.

## 2026-06-17 — Безопасность БД + компактный unit_tests-отчёт (ветка)
- **commit:** `ff0aef8` (gitignore), `446511a` (unit_tests report)
- **что:**
  - `.gitignore`: добавлены `data/`, `*.db`, `*.sqlite*`, `venv/`. БД (`data/words.db`) хранит секрет — **vf_proxy с кредами** в `app_settings`; была untracked, но не игнорилась → риск случайного коммита. Проверено: БД **никогда не была в git** (ни в дереве, ни в истории; в истории лишь старый `data/words.json` без кредов).
  - `_run_unit_tests_report`: вместо verbose-простыни (резалась Telegram'ом) — компактная сводка `✅ unit tests: N/N OK` или `❌ X/N OK, K failed:` + имена упавших + строка db smoke.
- **важно по секретам:** прокси-креды светились один раз в выводе harness этой сессии; рекомендовано сменить пароль прокси у провайдера. Прокси в коде печатать только через `_mask_proxy`.

## 2026-06-17 — Этап 3: вынесен verb_forms_parsing (бывш. vf) (ветка)
- **commit:** `f730947`
- **что:** пакет `bot/verb_forms_parsing/` — `parser.py` (fetch/parse/compare/apply + прокси-хелперы + vf_* ключи) и `handlers.py` (`_vf_menu_markup`, `_send_vf_menu`, `_vf_run_dry_task` + общие `VF_RUNNING_TASKS`/`VF_STOP_FLAGS`). Роутинг кнопок parse_VF остаётся в `settings_menu_choice` (main), импортирует пакет обратно. **DB-имена (таблица `vf_checks`, ключи `vf_*`) не трогали** — без миграции, по решению владельца.
- **проверка:** parser-функции, меню, ре-экспорт; критично — `VF_STOP_FLAGS` один объект в main и handlers (стоп работает через границу модуля); 49 тестов. main.py 5417 → 5115.
- **Этап 3 — независимые фичи (jobs, dump, vf) ВСЕ закрыты.** Остались `synonyms`/`db_search` — после quiz/edit (Этап 4).

## 2026-06-17 — Этап 3: вынесен независимый dump (ветка)
- **commit:** `8090408`
- **что:** `common/dumps.py` (файловые хелперы `_create_*_dump_file`), `jobs/auto_dump.py` (`_auto_dump_job`, `_configure_auto_dump`), `handlers/dump.py` (`dump_menu_choice`, `auto_dump_choice`, `_dump_menu_keys`, `_auto_dump_keys`). Направление импортов handlers→jobs→common, без циклов. Создан пакет `bot/handlers/`.
- **проверка:** реальный `_create_dump_file` создаёт дамп, клавиатуры/configure работают, 49 тестов. main.py 5578 → 5417.
- **Этап 3 остаток:** `vf` (независимый, крупный); `synonyms`/`db_search` (после quiz/edit, Этап 4).

## 2026-06-17 — Этап 3: ui/formatting.py + quiz-клавиатуры (ветка)
- **commit:** `7d0296a` (ui/formatting.py — все quiz/answer-форматтеры + хелперы), `08b31bc` (quiz-клавиатуры в ui/keyboards.py)
- **что:** вынесены форматтеры вопросов/фидбэка (ru_de/de_ru/article/pl_sg/weak), `_format_word_card`, `_format_learning_preview_prompt` + хелперы (`_entry_translation`, `_expected_partizip2`, `_article_*`, `_status_text`, `_nomen_with_edge_emoji`, `_append_hint_lines`, `_is_pl_sg_eligible`); и quiz-клавиатуры (`_quiz_markup`, `_quiz_idle_markup`, `_weak_article_markup`, `_edit_fields_markup`, `_type_choice_markup`, `_quiz_edit_root_markup`). `_check_ru_de_strict` остался в main (заимпортил `_article_matches`/`_expected_partizip2`).
- **проверка:** каждый форматтер прогнан на nomen/verb/adjektiv (ok=True/False) на копии БД; все клавиатуры вызваны; 49 тестов. main.py 5949 → 5578.
- **фундамент готов:** common/{lang,settings,session,txlog,navigation}, ui/{keyboards,formatting}, jobs/{reports,reminders}. Дальше — **quiz HANDLERS+prompts** (большой «домино» с локальными импортами для learning/flags/edit), затем weak/learning/synonyms/db_search/dump/vf.

## 2026-06-17 — Этап 3: jobs/ + common/navigation.py (ветка)
- **commit:** `f19e3b5` (jobs/reports.py + jobs/reminders.py), `c59d8b7` (common/navigation.py)
- **что:** вынесены фоновые джобы (FSRS daily/peak отчёты + retention-форматтеры; learning-reminder) и общие навигационные хелперы (`_action_key_from_text`, `_reply_text_with_retry`, `_back_to_main_menu`, `_back_to_tests`, `_clear_last_answer`). Поведение 1:1.
- **проверка:** `import bot.main` OK, рантайм-вызовы, 49 тестов. main.py 6304 → 5949.
- **блокер найден:** хендлеры (synonyms/db_search/weak/learning) зависят от quiz prompt-сендеров (`_send_*_prompt`) и `_return_from_synonyms`. Значит **quiz/prompt-модуль нужно вынести первым** (это «домино») — он в очереди следующим. auto_dump оставлен в main (поедет с dump-хендлером).

## 2026-06-16 — words.updated_at + скрипт миграции + чистка доков от SM2 (ветка)
- **commit:** `42aa314` (код), доки — локально без коммита
- **что:**
  - `words.updated_at` (для мультиюзер-синка изменённых слов): миграция добавляет колонку (константный дефолт `''`, т.к. SQLite запрещает CURRENT_TIMESTAMP в ADD COLUMN) и backfill'ит существующие строки в `created_at`. Бамп `updated_at=CURRENT_TIMESTAMP` при `add_word`/`update_word_fields`/`update_word_translations`.
  - `scripts/migrate_db.py` — тонкий идемпотентный раннер `init_db` для сервера (`python3 -m scripts.migrate_db --db ./data/words.db`), печатает counts до/после.
  - доки (`business_logic.md`, `architecture.md`, `database_schema.md`, `info_ru/uk.md`, `tests_ru.md`) очищены от `SM2`→`FSRS`. `log.md`/`refactor_plan.md` оставлены (там «sm2» — история переименования).
- **проверка:** на копии живой БД: миграция (sm2→fsrs ключи + updated_at) без потери данных (counts неизменны), `updated_at` backfilled 737/737==created_at, правка слова бампает, новое слово получает; 49 тестов зелёные.

## 2026-06-16 — SM2→FSRS полное переименование (код + БД), ветка
- **commit:** `14ab0df` (на ветке)
- **что:** все `sm2/SM2` → `fsrs/FSRS` по коду и тестам (функции, константы, локальные, in-memory stat/context-ключи, `test_sm2_logic.py`→`test_fsrs_logic.py`). **БД-ключи настроек тоже переименованы** (`sm2_report_utc_hour`→`fsrs_report_utc_hour` и др., 5 шт.) + миграция в `init_db`/`_ensure_schema`: старые ключи копируются в новые с сохранением значений, идемпотентно.
- **проверка (по запросу владельца):** на копии живой БД — миграция перенесла все 5 ключей со значениями, данные целы (words 737, progress 1096, settings 21 без изменений); `_fsrs_report_hour_utc` читает мигрированное значение; `fsrs_due_forecast`/`update_fsrs`/report-хелперы/`_pick_due_entry`/клавиатуры/stat-ключи работают; 49 тестов зелёные; `import bot.main` OK.
- **примечание:** доки (`business_logic.md` и др.) ещё содержат «SM2» в описаниях алгоритма — это документация, не код/БД; обновить отдельно при желании.

## 2026-06-16 — Рефакторинг main.py: Этап 1 (ветка refactor/split-main-stage1)
- **commit:** `b47078c`, `5993d83`, `6494935`, `67a7ad6`, `512e57a` (на ветке `refactor/split-main-stage1`, не в main)
- **файлы:** `bot/app_config.py`, `bot/labels.py` (new), `bot/ctx_keys.py` (new), `bot/common/{__init__,lang,settings,txlog}.py` (new), `bot/ui/{__init__,keyboards}.py` (new), `bot/main.py`
- **что:** структурный вынос без изменения поведения:
  1. P0/P1: `ctx_keys.py`, `labels.py`; owner/lang-ключи + дефолты/job-names/warmup → `app_config.py`.
  2. `common/lang.py` — язык/режимы.
  3. `common/settings.py` — геттеры настроек + topic-filter.
  4. `common/txlog.py` — snapshot/undo-лог.
  5. `ui/keyboards.py` — меню-клавиатуры (core).
- **почему:** читабельность + заготовка общего `core`.
- **проверка:** каждый коммит — `import bot.main` OK, рантайм-harness (клавиатуры/lang/settings/topic) на копии БД OK, 38 тестов OK. main.py 6811 → ~6535.
- **примечание:** форматтеры (`_format_*`, 27 шт.) и фиче-клавиатуры завязаны на фичи → переносим с их хендлерами на Этапах 3–4 (см. refactor_plan.md, раздел «Статус»). `ui/formatting.py` сейчас НЕ делаем.

## 2026-06-16 — План рефакторинга main.py (docs/refactor_plan.md)
- **commit:** локально (без коммита)
- **файлы:** `docs/refactor_plan.md`, `docs/log.md`
- **что:** оформлен поэтапный план разбиения `main.py` (~6800 строк) на модули (`labels`, `ctx_keys`, `ui/`, `common/`, `handlers/`, `jobs/`) — структурный рефактор без изменения поведения, по коммитам, от листьев к связанным хендлерам, с чек-листом и стратегией против циклических импортов.
- **почему:** один файл на 6800 строк нечитаем; заодно готовит общий `core` для будущего мультиюзер-бота.
- **проверка:** план опирается на фактический инвентарь функций main.py; код не менялся.

## 2026-06-14 — Фикс перехода clarify_fields в квизе + унификация подписи
- **commit:** подготовлен в `main` (пуш — за владельцем)
- **файлы:** `bot/main.py`, `bot/app_config.py`
- **что:** 1) исправлен баг в `_start_quiz_edit`: второй `_reply_text_with_retry` вызывался без `update.message` → `TypeError` → не было перехода в `ADD_EDIT`. Чинит «clarify_fields» во всех квиз-режимах (ru_de/de_ru/pl_sg/article/weak) и в learning. 2) Унифицирована подпись кнопки: `AI_CLARIFY_FIELD` и `QUIZ_FIELDS_TEXT` теперь обе = `"clarify_fields"` (было «Clarify field» и «fields»); обновлены и тексты-подсказки add-флоу.
- **почему:** вход в редактирование полей из квиза не работал; в db_search та же функция называлась иначе.
- **проверка:** `import bot.main` OK; 38 тестов проходят; обе константы не сверяются в одном хендлере (коллизии нет). UI-флоу проверить вручную в Telegram.

## 2026-06-14 — Доки: уточнения от владельца + input_err
- **commit:** локально (без коммита)
- **файлы:** `docs/business_logic.md`, `docs/architecture.md`, `docs/database_schema.md`, `docs/navigation_map.md`
- **что:** закрыты `Unknown` по словам владельца — `parse_VF` (парсинг verbformen.de через прокси), precheck написания через AI перед AI-парсингом, warmup learning `(0,0,0)` (preview → 1 раз по порядку → 2 раза вразнобой → внутридневные интервалы → FSRS), синонимы (одинаковый перевод → засчитывается любой), learning-напоминания помечены как недоделанные (TODO). Добавлен раздел §7a про `input_err` (откатывает штраф и засчитывает верный ответ; ease не падает).
- **почему:** наполнение доков фактами, которые не выводятся из кода напрямую.
- **проверка:** `_learning_schedule_seconds = [0,0,0] + learning_intervals_sec` сверено по коду; `_apply_input_err` вычитан построчно.

## 2026-06-14 — Документация проекта (docs/*)
- **commit:** локально (без коммита на момент записи)
- **файлы:** `docs/architecture.md`, `docs/navigation_map.md`, `docs/database_schema.md`, `docs/business_logic.md`, `docs/log.md`
- **что:** созданы документы по архитектуре, карте навигации/FSM, схеме БД, бизнес-логике и этот лог. Содержимое выведено из кода; непроверенные места помечены `Unknown`. Код не менялся.
- **почему:** запрос на структурированную документацию для дальнейшего рефакторинга (дробление `main.py`).
- **проверка:** факты сверены с `bot/storage.py` (`init_db`, `update_sm2`, `select_due`), `bot/app_config.py` (`range(41)`), `bot/main.py` (ConversationHandler, билдеры меню).

## 2026-06-14 — Фикс NameError при добавлении Nomen + вынос casing-хелперов
- **commit:** `20fa522` (на `main`, локально; пуш — за владельцем)
- **файлы:** `bot/text_utils.py`, `bot/storage.py`, `bot/main.py`
- **что:** `cap_word`/`low_word` вынесены в `text_utils.py` как единый источник. `storage.py` импортирует `cap_word`; `main.py` импортирует их под прежними алиасами `_cap_word`/`_low_word` (вызовы не трогались).
- **почему:** `storage._normalize_word_entry_for_storage` (ветка `nomen`) звала `_cap_word`, определённую только в `main.py` → `NameError` при добавлении существительных (с коммита `eb418a0` от 2026-06-09). Verb/adjektiv/phrase не задеты.
- **проверка:** нормализация регистра по типам (nomen — с заглавной, прочее и переводы — строчные) проверена; полный набор тестов — 38 passed (был 21 error).

## 2026-06-14 — Фикс порядка параметров в select_due (очередь FSRS)
- **commit:** `6252fe9` (на `main`, запушен владельцем в `origin/main`)
- **файлы:** `bot/storage.py`
- **что:** исправлен порядок параметров запроса в `select_due`: `[user_id, user_id, mode, user_id]` (было `[user_id, mode, user_id, user_id]`).
- **почему:** из-за перепутанных параметров JOIN к `progress` не срабатывал (`p.user_id` получал строку режима), `p.due` всегда был NULL → `COALESCE(due, today)=today` → в очередь попадали все «введённые» слова независимо от интервала (пример: слово с `due=2036` показывалось сегодня). Утренний прогноз (`sm2_due_forecast`) был корректен, отсюда расхождение «уведомление vs очередь».
- **проверка:** на копии живой БД очередь `ru_de` 633→173 (совпало с прогнозом), слово `Schrank` (due 2036) ушло из очереди; будущих de_ru слов в очередь не утекает (0).

## До 2026-06-14 (из git history, кратко)
- `35e7778` Normalize AI preview and precheck display
- `a5692d2` Lowercase translations before storing words
- `eb418a0` Normalize stored word casing by type *(внёс баг `_cap_word`, исправлен в `20fa522`)*
- `93eaeb0` Show learning active list and DB card study status
- `e13f2a4` Include image/example in learning due entry query
> Полная история — `git log`.
