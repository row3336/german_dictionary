"""todo T8 — interactive HTML review companion: rendering and apply-JSON compatibility."""

import json
import re
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


def _function_body(html, name):
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", html)
    if not match:
        raise AssertionError(f"JS function not found: {name}")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(html)):
        if html[index] == "{":
            depth += 1
        elif html[index] == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1:index]
    raise AssertionError(f"Unclosed JS function: {name}")


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

    def test_conjugation_block_is_in_diff_and_html(self):
        # owner request (live run 11.07): the Präsens block must be visible in
        # the review like every other field.
        from bot.verb_forms_parsing.vf_file import _DIFF_FIELDS
        self.assertIn("conjugation_praesens", _DIFF_FIELDS)
        conj = "ich fliege\ndu fliegst\ner/sie/es fliegt\nwir fliegen\nihr fliegt\nsie/Sie fliegen"
        card = {
            "type": "verb", "infinitive": "fliegen", "russian": "летать",
            "conjugation_praesens": conj,
            "_review": {
                "query": "fliegen", "status": "done", "flags": [], "error": "",
                "headword": "fliegen", "word_id": "w-2",
                "diff": {"conjugation_praesens": {"current": "", "vf": conj}},
            },
        }
        html = render_review_html(_doc([card]))
        self.assertIn("ich fliege", html)
        self.assertIn("BLOCK_FIELDS", html)  # rendered as a whole multiline unit

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

    def test_checkbox_and_chip_handlers_update_locally_without_render(self):
        html = render_review_html(_doc([_recheck_card()]))

        for helper in ("setWordAccepted", "setFieldAccepted", "setChipEnabled"):
            body = _function_body(html, helper)
            self.assertNotIn("render()", body)
            self.assertIn("updateCounters()", body)

        self.assertIn("setWordAccepted(m, card, e.target.checked)", html)
        self.assertIn("setFieldAccepted(f, tr, cb.checked)", html)
        self.assertIn("setChipEnabled(ch, el, !ch.on)", html)
        self.assertNotIn("m.accepted = e.target.checked; render()", html)
        self.assertNotIn("f.accepted = cb.checked; render()", html)
        self.assertNotIn("ch.on = !ch.on;\n      render()", html)

    def test_chip_enter_commits_locally_and_escape_still_cancels(self):
        html = render_review_html(_doc([_recheck_card()]))
        body = _function_body(html, "makeChip")

        self.assertIn('if (ev.key === "Enter")', body)
        self.assertIn("ev.preventDefault()", body)
        self.assertIn("finishEdit(false)", body)
        self.assertIn('if (ev.key === "Escape")', body)
        self.assertIn("finishEdit(true)", body)
        self.assertIn("inp.value.trim() || ch.text", body)
        self.assertNotIn("render()", body)

    def test_single_and_block_enter_share_trimmed_blur_commit(self):
        html = render_review_html(_doc([_recheck_card()]))
        body = _function_body(html, "wireEditable")

        self.assertIn("const commit = () =>", body)
        self.assertIn(".trim()", body)
        self.assertIn("el.onblur = commit", body)
        self.assertIn('if (ev.key !== "Enter") return', body)
        self.assertIn("ev.preventDefault()", body)
        self.assertIn("commit()", body)
        self.assertNotIn("render()", body)
        self.assertIn("wireEditable(dv, f, true)", html)
        self.assertIn("wireEditable(sp, f, false)", html)

    def test_build_apply_wire_shape_remains_unchanged(self):
        html = render_review_html(_doc([_recheck_card()]))
        body = _function_body(html, "buildApply")

        self.assertIn("JSON.parse(JSON.stringify(m.word))", body)
        self.assertIn("card[f.name] = f.current", body)
        self.assertIn("job_id: DATA.job_id", body)
        self.assertIn("topic: DATA.topic", body)
        self.assertIn("generated_at: DATA.generated_at", body)
        self.assertIn('edited_in: "html-review"', body)
        self.assertIn("words }, null, 2", body)


if __name__ == "__main__":
    unittest.main()
