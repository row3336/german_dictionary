"""K17: source case and existing contracts on synthetic phrase displays."""

import ast
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from bot.handlers import db_search
from bot.handlers.quiz import _check_ru_de_strict
from bot.oral import view
from bot.text_utils import low_word, md_escape, normalize_answer, normalize_lookup_text
from bot.ui import formatting as F


def phrase(german, **extra):
    return {"id": "synthetic", "type": "phrase", "german": german,
            "russian": "Перевод", "ukrainian": "Переклад", "english": "Translation",
            "topic": "1", **extra}


class PhraseCaseTest(unittest.TestCase):
    VALUES = ("Das Licht geht nicht.", "den Haushalt machen", "Auf Wiedersehen")

    def test_c01_preview_source_case(self):
        for text in self.VALUES:
            with self.subTest(text=text):
                self.assertEqual(F._format_learning_preview_prompt(phrase(text), "ru"),
                                 f"*{text}*\n>>    Перевод (phrase)")

    def test_c01_feedback_source_case_correct_and_wrong(self):
        for text in self.VALUES:
            for ok in (True, False):
                with self.subTest(text=text, ok=ok):
                    self.assertEqual(F._format_feedback_ru_de(phrase(text), ok),
                                     f"{F._status_text(ok)} >>>     *{text}*")

    def test_c01_matches_source_case(self):
        for text in self.VALUES:
            with self.subTest(text=text):
                self.assertEqual(db_search._format_match_line(phrase(text), "ru"),
                                 f"phrase - {text} Перевод")

    def test_c04_trim_empty_unicode_single_escape(self):
        for value in (" \tDas Licht geht nicht.\n", "", None, " \t\n ",
                      "ÄÖÜß 👋 McDonald’s: 'Auf_Wiedersehen*`!'", "bereits klein"):
            raw = str(value or "").strip()
            escaped = md_escape(raw)
            with self.subTest(value=value):
                entry = phrase(value)
                self.assertEqual(F._format_learning_preview_prompt(entry, "ru"),
                                 f"*{escaped}*\n>>    Перевод (phrase)")
                for ok in (True, False):
                    self.assertEqual(F._format_feedback_ru_de(entry, ok),
                                     f"{F._status_text(ok)} >>>     *{escaped}*")
                # Matches retains its raw code-block/file-list contract.
                self.assertEqual(db_search._format_match_line(entry, "ru"),
                                 " ".join(x for x in ("phrase", "-", raw, "Перевод") if x))

    def test_c02_oral_both_directions_hide_then_reveal(self):
        entry = phrase("Auf Wiedersehen")
        for mode in ("phrase_ru_de", "phrase_de_ru"):
            frame = {"phase": "initial_hidden", "mode": mode,
                     "hidden": {"entry": entry}, "pending": None}
            before = copy.deepcopy(frame)
            hidden, options = view.message(frame, "en")
            self.assertEqual(frame, before)
            self.assertEqual(options["parse_mode"], "Markdown")
            self.assertNotIn(">>>", hidden)
            visible, absent = (("Перевод", "Auf Wiedersehen") if mode == "phrase_ru_de"
                               else ("Auf Wiedersehen", "Перевод"))
            self.assertIn(visible, hidden)
            self.assertNotIn(absent, hidden)
            frame.update(phase="grading", hidden=None, pending={"entry": entry})
            revealed, _ = view.message(frame, "en")
            self.assertEqual(revealed, f">>>    *{absent}*")

    def test_c02_existing_card_question_and_learning_list(self):
        entry = phrase("Auf Wiedersehen")
        self.assertEqual(F._format_de_ru_question(entry), "Auf Wiedersehen\n>>    ❓")
        self.assertEqual(F._format_feedback_de_ru(entry, True, "ru"),
                         f"{F._status_text(True)} >>>     *перевод*")
        self.assertIn("german: Auf Wiedersehen", F._format_word_card(entry))
        self.assertEqual(F._format_topic_learning_line(entry), "[1] - [Auf Wiedersehen]")

    def test_c03_other_pos_keep_existing_rendering(self):
        cases = (
            ({"type": "nomen", "german": "HAUS", "article": "das", "plural": "HÄUSER"},
             "*🟢 das Haus 🟢*\n_pl. die Häuser_", "🟢 das Haus 🟢", "nomen - das Haus Häuser Перевод"),
            ({"type": "verb", "german": "GEHEN", "infinitive": "GEHEN", "prateritum": "GING",
              "partizip2": "GEGANGEN", "reflexive_case": "none", "hilfsverb": "ist"},
             "*gehen*\n_ging, ist gegangen_", "gehen", "verb - gehen ging gegangen Перевод"),
            ({"type": "adjektiv", "german": "GUT", "komparativ": "BESSER", "superlativ": "AM BESTEN"},
             "*gut*\n_  besser, am besten_", "gut", "adjektiv - gut besser am besten Перевод"),
            ({"type": "praeposition", "german": "BEI"}, "*bei*", "bei", "praeposition - bei Перевод"),
            ({"type": "pronomen", "german": "ICH"}, "*ich*", "ich", "pronomen - ich Перевод"),
        )
        for fields, preview, answer, match in cases:
            with self.subTest(pos=fields["type"]):
                entry = phrase("unused") | fields
                self.assertEqual(F._format_learning_preview_prompt(entry, "ru"),
                                 preview + f"\n>>    Перевод ({fields['type']})")
                self.assertEqual(F._format_feedback_ru_de(entry, True).splitlines()[0],
                                 f"{F._status_text(True)} >>>     *{answer}*")
                self.assertEqual(db_search._format_match_line(entry, "ru"), match)

    def test_c03_normalizers_and_grading_stay_case_insensitive(self):
        for normalize in (low_word, normalize_answer, normalize_lookup_text):
            self.assertEqual(normalize("  Das Licht GEHT nicht.  "), "das licht geht nicht.")
        entry = phrase("Das Licht geht nicht.")
        with patch("bot.handlers.quiz._synonym_entries", return_value=[entry]):
            self.assertTrue(_check_ru_de_strict(entry, "DAS LICHT GEHT NICHT.")[0])
            self.assertFalse(_check_ru_de_strict(entry, "Das Licht geht.")[0])

    def test_c05_examples_translations_status_personal_layer(self):
        entry = phrase("Auf Wiedersehen", example="Ein Beispiel_*\nZweites Beispiel",
                       image="Bild_*", my_translation="Mein_Text")
        for lang in ("ru", "uk", "en"):
            self.assertEqual(F._format_learning_preview_prompt(entry, lang),
                             "*Auf Wiedersehen*\n>>    Mein\\_Text (phrase)\n\nEin Beispiel\\_\\*")
        for ok in (True, False):
            self.assertEqual(F._format_feedback_ru_de(entry, ok),
                             f"{F._status_text(ok)} >>>     *Auf Wiedersehen*" +
                             ("" if ok else "\n\nBild\\_\\*"))
        self.assertEqual(db_search._format_match_line(entry, "en"), "phrase - Auf Wiedersehen Mein_Text")

    def test_c05_display_does_not_open_database_or_mutate_input(self):
        entry = phrase("Das Licht geht nicht.")
        before = copy.deepcopy(entry)
        with patch("sqlite3.connect", side_effect=AssertionError("display opened DB")):
            F._format_learning_preview_prompt(entry, "ru")
            F._format_feedback_ru_de(entry, True)
            F._format_feedback_ru_de(entry, False)
            db_search._format_match_line(entry, "ru")
        self.assertEqual(entry, before)

    def test_c02_legacy_search_formatter_has_no_active_call(self):
        tree = ast.parse(Path(db_search.__file__).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_format_db_search_card"]
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
