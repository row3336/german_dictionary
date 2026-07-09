"""Centralized string keys for context.user_data.

P0 of the main.py refactor: this module just defines the keys. Call sites in
main.py are migrated to use these constants gradually, as each handler group
is extracted into its own module. Not yet wired everywhere.

Some keys are dynamic and built from a mode at runtime (kept as helpers):
- session "seen" set:        f"session_seen_{mode}"
- fsrs cycle queue bucket:     "fsrs_cycle_queue" (dict keyed by mode)
- weak drill buckets:         "weak_drill_queue" / "weak_drill_counts" / "weak_drill_since_last" (dicts keyed by mode)
- retry buckets:              "retry_queue" / "retry_counts" / "retry_since_last" (dicts keyed by mode)
"""

# add-flow
ADD_QUEUE = "add_queue"
PENDING_ADD_QUEUE = "pending_add_queue"
PENDING_ADD_STATS = "pending_add_stats"
PENDING_ENTRY = "pending_entry"
PENDING_WORD = "pending_word"
PRECHECK_EXISTING = "precheck_existing"
PRECHECK_FILTERED = "precheck_filtered"
PRECHECK_ITEMS = "precheck_items"
LAST_AI_PROMPT = "last_ai_prompt"

# quiz / answers
QUIZ_ENTRY = "quiz_entry"
QUIZ_IS_RETRY = "quiz_is_retry"
QUIZ_IS_WEAK_DRILL = "quiz_is_weak_drill"
QUIZ_LAST_ANSWERED_ENTRY = "quiz_last_answered_entry"
LAST_QUIZ_IDS = "last_quiz_ids"
LAST_ANSWERED_ENTRY = "last_answered_entry"
LAST_ANSWERED_MODE = "last_answered_mode"
LAST_ANSWERED_OK = "last_answered_ok"
LAST_ANSWERED_SRS = "last_answered_srs"
SESSION_MODE = "session_mode"

# retry / weak drill (bucket dicts)
RETRY_QUEUE = "retry_queue"
RETRY_COUNTS = "retry_counts"
RETRY_SINCE_LAST = "retry_since_last"
WEAK_DRILL_QUEUE = "weak_drill_queue"
WEAK_DRILL_COUNTS = "weak_drill_counts"
WEAK_DRILL_SINCE_LAST = "weak_drill_since_last"

# weak answers
LAST_WEAK_ANSWERED_ENTRY = "last_weak_answered_entry"
LAST_WEAK_ANSWERED_FIELD = "last_weak_answered_field"
LAST_WEAK_ANSWERED_OK = "last_weak_answered_ok"

# learning
LEARNING_RECENT_IDS = "learning_recent_ids"  # last shown word ids, for later-round show spacing
LEARNING_PREVIEW_MODE = "learning_preview_mode"
LAST_LEARNING_ENTRY_ID = "last_learning_entry_id"
LAST_LEARNING_OK = "last_learning_ok"
LAST_LEARNING_PREV_STEP = "last_learning_prev_step"

# edit fields
EDIT_FIELD = "edit_field"
EDIT_SOURCE = "edit_source"
EDIT_STAGE = "edit_stage"
EDIT_RETURN_STATE = "edit_return_state"
QUIZ_EDIT_ENTRY = "quiz_edit_entry"

# flags
FLAG_RETURN_STATE = "flag_return_state"
FLAG_WORD_ID = "flag_word_id"

# settings / topic
ALGO_EDIT_RETURN = "algo_edit_return"
ALGO_SETTING_EDIT = "algo_setting_edit"
TOPIC_RETURN = "topic_return"

# db search
DB_SEARCH_TOPIC_PICK = "db_search_topic_pick"
DB_SEARCH_WEAK_IDS = "db_search_weak_ids"
DB_SEARCH_WORDS_LISTS_MENU = "db_search_words_lists_menu"

# synonyms
SYN_ANCHOR_ID = "syn_anchor_id"
SYN_AUTO_CANDIDATES = "syn_auto_candidates"
SYN_RETURN_STATE = "syn_return_state"

# verbformen (parse_VF)
VF_MENU = "vf_menu"
VF_WAITING = "vf_waiting"
