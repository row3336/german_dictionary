"""todo T8 — interactive HTML review companion: rendering and apply-JSON compatibility."""

import json
import unittest

from _telegram_stub import install

install()

from bot.verb_forms_parsing.vf_file import parse_review_document  # noqa: E402
from bot.verb_forms_parsing.vf_review_html import (  # noqa: E402
    render_review_html,
    review_html_filename,
)


def _doc(words):
    return {
        "job_id": 9,
        "topic": "05 Essen",
        "generated_at": "2026-07-11 10:00:00",
        "instructions": "…",
        "words": words,
    }


def _recheck_card(**overrides):
    card = {
        "type": "nomen",
        "article": "das",
        "german": "Haus",
        "russian": "дом, здание",
        "example": "Ich geh nach Hause.\nEuer Haus ist groß.",
        "cefr_level": "A1",
        "_review": {
            "query": "Haus",
            "status": "done",
            "flags": [],
            "error": "",
            "headword": "Haus",
            "word_id": "w-1",
            "diff": {
                "russian": {"current": "дом", "vf": "дом, здание"},
                "cefr_level": {"current": "", "vf": "A1"},
            },
        },
    }
    card.update(overrides)
    return card


class RenderReviewHtmlTest(unittest.TestCase):
    def test_embeds_data_and_words(self):
        html = render_review_html(_doc([_recheck_card()]))
        self.assertNotIn("__VF_REVIEW_DATA__", html)
        self.assertIn('"german": "Haus"', html)
        self.assertIn('"word_id": "w-1"', html)
        # dark theme + approved layout markers
        self.assertIn("--bg: #16191d", html)
        self.assertIn("Скачать apply-JSON", html)

    def test_script_close_tag_is_escaped(self):
        # A literal "</script>" inside a value must not terminate the data block
        # and truncate the page.
        card = _recheck_card(example="böse </script> Zeile")
        html = render_review_html(_doc([card]))
        self.assertIn("<\\/script>", html)
        # the only real closing tag left is the template's own one
        self.assertEqual(html.count("</script>"), 1)

    def test_filename(self):
        self.assertEqual(review_html_filename(9), "vf_review_job9.html")

    def test_html_export_shape_is_apply_compatible(self):
        # The page exports {job_id, topic, generated_at, edited_in, words:[card]}
        # where each card keeps _review (word_id) and carries the chosen values.
        # parse_review_document must read it exactly like a hand-edited JSON.
        exported = {
            "job_id": 9,
            "topic": "05 Essen",
            "generated_at": "2026-07-11 10:00:00",
            "edited_in": "html-review",
            "words": [
                {
                    "type": "nomen",
                    "article": "das",
                    "german": "Haus",
                    "russian": "дом",  # owner rejected the new meaning
                    "cefr_level": "A1",
                    "_review": {"word_id": "w-1", "status": "done"},
                }
            ],
        }
        entries, issues = parse_review_document(json.dumps(exported, ensure_ascii=False))
        self.assertEqual(issues, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "w-1")  # recheck card targets the DB word
        self.assertEqual(entries[0]["russian"], "дом")
        self.assertNotIn("_review", entries[0])


if __name__ == "__main__":
    unittest.main()
