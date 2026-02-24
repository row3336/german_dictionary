# Тесты логики (SM2 и session-retry)

Этот документ объясняет, что именно проверяют тесты и как их запускать.

## Что проверяется

### Session retry (tests/test_retry_logic.py)

1. `test_no_queue`  
   Если очередь пустая, retry не показывается.

2. `test_gap_not_reached`  
   Если `since_last < gap`, retry не показывается.

3. `test_take_retry_when_gap_reached`  
   Если `since_last >= gap`, из очереди берётся слово, счетчик сбрасывается.

4. `test_missing_entry_removes_count`  
   Если слово уже удалено из БД, оно удаляется из счётчиков.

5. `test_register_wrong_adds_queue`  
   Ошибка на SM2 добавляет слово в retry‑очередь.

6. `test_register_ok_no_change`  
   Правильный ответ не добавляет слово в retry.

7. `test_retry_requeue_until_max`  
   Retry‑слово возвращается в очередь, пока не достигнут лимит повторов.

8. `test_retry_stops_at_max`  
   После `max_repeats` слово удаляется из retry‑счётчика.

### SM2 (tests/test_sm2_logic.py)

1. `test_select_due_includes_new_word`  
   Новое слово без прогресса считается due.

2. `test_select_due_excludes_future_due`  
   Если `due` в будущем, слово не возвращается в `select_due`.

3. `test_update_sm2_wrong_moves_due_tomorrow`  
   Ошибка сбрасывает `repetitions` и ставит `due` на завтра.

4. `test_update_sm2_correct_increases_interval`  
   Правильный ответ увеличивает интервал и `due`.

## Как запускать

Точечно:
```bash
python3 -m unittest -v tests.test_retry_logic
python3 -m unittest -v tests.test_sm2_logic
```

Если общий запуск `python3 -m unittest -v` не находит тесты, используйте команды выше.
