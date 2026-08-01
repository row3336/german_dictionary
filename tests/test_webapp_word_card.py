"""Mini App M7 step 1 — the word card (miniapp.md §7.5, §8.4a, §11).

The card is the app's most reused component: the dictionary shows it and, from M4,
so does the quiz feedback. Two rules are enforced here rather than in CSS, because
a frontend `if` is easy to forget and a missing key cannot be rendered by accident:

  * empty data is absent from the payload, never present-and-blank;
  * German grammar travels as German - the card does not translate `praeteritum`
    or `dativ` into the user's language.

Plus the media rule the owner reinstated on 2026-08-01: a personal example wins,
a blank one falls through to the shared word, and neither leaks between users.
"""

import unittest

from _webapp_helpers import TempDbMixin

from bot.storage import add_word, set_user_dict_lang, set_user_word_media
from bot.webapp.serializers import (
    conjugation_of,
    forms_of,
    gender_of,
    headword,
    translation_of,
    word_card,
)
from bot.webapp.word_repo import (
    apply_media_with_fallback,
    get_study_state,
    get_word_full,
    load_card_sources,
)

USER = 111
OTHER = 222

PRAESENS = "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen"
PRAETERITUM = "ich ging\ndu gingst\ner/sie/es ging\nwir gingen\nihr gingt\nsie/Sie gingen"


def entry(**overrides):
    base = {
        "id": "w1",
        "type": "nomen",
        "article": "der",
        "german": "Tisch",
        "plural": "Tische",
        "infinitive": "",
        "prateritum": "",
        "partizip2": "",
        "komparativ": "",
        "superlativ": "",
        "russian": "стол",
        "ukrainian": "стіл",
        "english": "table",
        "image": "",
        "example": "",
        "topic": "1",
        "preposition_type": "",
        "case_type": "",
    }
    base.update(overrides)
    return base


class GenderTest(unittest.TestCase):
    def test_reads_the_gender_from_the_article(self):
        for article, gender in (("der", "m"), ("die", "f"), ("das", "n")):
            with self.subTest(article=article):
                self.assertEqual(gender_of(entry(article=article)), gender)

    def test_a_word_without_an_article_has_no_gender(self):
        self.assertEqual(gender_of(entry(type="verb", article="")), "")


class HeadwordTest(unittest.TestCase):
    def test_a_verb_leads_with_its_infinitive(self):
        self.assertEqual(headword(entry(type="verb", german="geht", infinitive="gehen")), "gehen")

    def test_a_verb_without_an_infinitive_falls_back(self):
        self.assertEqual(headword(entry(type="verb", german="gehen", infinitive="")), "gehen")

    def test_other_types_lead_with_the_german_word(self):
        self.assertEqual(headword(entry()), "Tisch")


class FormsTest(unittest.TestCase):
    def test_a_noun_carries_its_plural(self):
        self.assertEqual(forms_of(entry()), {"plural": "Tische"})

    def test_a_verb_carries_its_past_forms(self):
        forms = forms_of(entry(type="verb", prateritum="ging", partizip2="gegangen"))
        self.assertEqual(forms, {"prateritum": "ging", "partizip2": "gegangen"})

    def test_an_adjective_carries_its_degrees(self):
        forms = forms_of(entry(type="adjektiv", komparativ="schneller", superlativ="am schnellsten"))
        self.assertEqual(forms, {"komparativ": "schneller", "superlativ": "am schnellsten"})

    def test_empty_forms_are_dropped_not_blanked(self):
        self.assertEqual(forms_of(entry(plural="")), {})

    def test_types_without_extra_forms_get_none(self):
        self.assertEqual(forms_of(entry(type="adverb")), {})


class TranslationTest(unittest.TestCase):
    def test_follows_the_dictionary_language(self):
        self.assertEqual(translation_of(entry(), "ru"), "стол")
        self.assertEqual(translation_of(entry(), "uk"), "стіл")
        self.assertEqual(translation_of(entry(), "en"), "table")

    def test_a_personal_translation_wins(self):
        # spec #7: the base column stays intact, display prefers the override.
        item = entry(my_translation="мой стол")
        self.assertEqual(translation_of(item, "ru"), "мой стол")

    def test_a_blank_personal_translation_falls_through(self):
        self.assertEqual(translation_of(entry(my_translation="  "), "ru"), "стол")


class ConjugationTest(unittest.TestCase):
    def test_splits_a_stored_block_into_pronoun_form_pairs(self):
        rows = conjugation_of(entry(type="verb", conjugation_praesens=PRAESENS))["praesens"]
        self.assertEqual(rows[0], ["ich", "gehe"])
        self.assertEqual(rows[-1], ["sie/Sie", "gehen"])
        self.assertEqual(len(rows), 6)

    def test_both_tenses_travel_when_both_are_stored(self):
        item = entry(
            type="verb", conjugation_praesens=PRAESENS, conjugation_praeteritum=PRAETERITUM
        )
        self.assertEqual(set(conjugation_of(item)), {"praesens", "praeteritum"})

    def test_a_missing_block_is_absent_not_empty(self):
        # Präteritum exists only for a closed list of verbs - that is normal data,
        # not a gap, and the card simply does not draw the section.
        item = entry(type="verb", conjugation_praesens=PRAESENS)
        self.assertNotIn("praeteritum", conjugation_of(item))

    def test_a_malformed_block_is_dropped_whole(self):
        for broken in ("ich gehe\ndu gehst", "ich gehe\nDU gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen"):
            with self.subTest(block=broken[:20]):
                self.assertEqual(conjugation_of(entry(conjugation_praesens=broken)), {})


class CardShapeTest(unittest.TestCase):
    def test_a_noun_card(self):
        card = word_card(entry(), "ru")
        self.assertEqual(card["german"], "Tisch")
        self.assertEqual(card["article"], "der")
        self.assertEqual(card["gender"], "m")
        self.assertEqual(card["forms"], {"plural": "Tische"})
        self.assertEqual(card["translation"], "стол")

    def test_a_preposition_keeps_its_german_facing_vocabulary(self):
        # `local` and `dativ` are shown as stored, in every interface language.
        card = word_card(
            entry(type="praeposition", article="", preposition_type="local", case_type="dativ"),
            "ru",
        )
        self.assertEqual(card["preposition_type"], "local")
        self.assertEqual(card["case_type"], "dativ")

    def test_nothing_empty_is_sent(self):
        card = word_card(entry(topic="", plural=""), "ru")
        for absent in ("forms", "media", "synonyms", "conjugation", "study", "topic"):
            self.assertNotIn(absent, card)

    def test_media_appears_only_when_filled(self):
        card = word_card(entry(example="Der Tisch ist alt."), "ru")
        self.assertEqual(card["media"], {"example": "Der Tisch ist alt."})

    def test_synonyms_exclude_the_word_itself(self):
        group = [entry(), entry(id="w2", german="Tafel", article="die")]
        card = word_card(entry(), "ru", synonyms=group)
        self.assertEqual(len(card["synonyms"]), 1)
        self.assertEqual(card["synonyms"][0]["german"], "Tafel")
        self.assertEqual(card["synonyms"][0]["gender"], "f")

    def test_a_word_without_a_group_has_no_synonyms_key(self):
        self.assertNotIn("synonyms", word_card(entry(), "ru", synonyms=[entry()]))

    def test_study_is_included_when_given(self):
        card = word_card(entry(), "ru", study={"state": "fsrs", "repetitions": 3})
        self.assertEqual(card["study"]["repetitions"], 3)


class MediaFallbackTest(TempDbMixin, unittest.TestCase):
    """The COALESCE(personal, shared) rule reinstated on 2026-08-01."""

    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.make_user(OTHER, "approved")
        self.word = add_word(
            self.db,
            {
                "id": "unique",
                "type": "nomen",
                "article": "der",
                "german": "Tisch",
                "plural": "Tische",
                "russian": "стол",
                "ukrainian": "",
                "english": "",
                "topic": "1",
                "example": "Der Tisch steht dort.",
                "image": "shared image",
            },
        )

    def _merged(self, user_id):
        stored = get_word_full(self.db, self.word["id"])
        return apply_media_with_fallback(self.db, user_id, stored)

    def test_a_guest_sees_the_shared_example_when_they_have_none(self):
        # The old rule showed an empty block here, though words.example is filled
        # for 769 of 907 words.
        self.assertEqual(self._merged(USER)["example"], "Der Tisch steht dort.")

    def test_a_personal_example_wins(self):
        set_user_word_media(self.db, USER, self.word["id"], example="Mein Tisch.")
        self.assertEqual(self._merged(USER)["example"], "Mein Tisch.")

    def test_one_users_media_never_reaches_another(self):
        set_user_word_media(self.db, USER, self.word["id"], example="Mein Tisch.")
        self.assertEqual(self._merged(OTHER)["example"], "Der Tisch steht dort.")

    def test_the_owner_sees_the_shared_word(self):
        self.assertEqual(self._merged(self.OWNER_ID)["image"], "shared image")


class StudyStateTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.word = self.add_noun("Tisch", "стол")

    def test_an_untouched_word_is_queued_for_learning(self):
        state = get_study_state(self.db, USER, self.word["id"])
        self.assertEqual(state["state"], "queue_learning")
        self.assertEqual(state["weak_forms"], [])
        self.assertEqual(state["due"], "")

    def test_a_studied_word_reports_its_schedule(self):
        from bot.storage import update_progress

        update_progress(self.db, user_id=USER, word_id=self.word["id"], mode="ru_de", quality=5)
        state = get_study_state(self.db, USER, self.word["id"])
        self.assertEqual(state["state"], "fsrs")
        self.assertGreaterEqual(state["repetitions"], 1)
        self.assertTrue(state["due"])

    def _mark_weak(self, user_id, field):
        """Miss a form until the bot flags it - one wrong answer is not enough."""
        from bot.storage import WEAK_ACTIVE_MIN_STREAK_WRONG, update_form_stats

        for _ in range(WEAK_ACTIVE_MIN_STREAK_WRONG):
            update_form_stats(self.db, user_id, self.word["id"], {field: False})

    def test_weak_forms_are_listed(self):
        self._mark_weak(USER, "article")
        self.assertEqual(get_study_state(self.db, USER, self.word["id"])["weak_forms"], ["article"])

    def test_a_form_answered_correctly_is_not_weak(self):
        from bot.storage import update_form_stats

        update_form_stats(self.db, USER, self.word["id"], {"plural": True})
        self.assertEqual(get_study_state(self.db, USER, self.word["id"])["weak_forms"], [])

    def test_weak_forms_are_per_user(self):
        self.make_user(OTHER, "approved")
        self._mark_weak(OTHER, "article")
        self.assertEqual(get_study_state(self.db, USER, self.word["id"])["weak_forms"], [])
        self.assertEqual(get_study_state(self.db, OTHER, self.word["id"])["weak_forms"], ["article"])


class LoadCardSourcesTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")

    def test_assembles_everything_the_card_needs(self):
        word = self.add_noun("Tisch", "стол")
        sources = load_card_sources(self.db, USER, word["id"])
        card = word_card(sources["entry"], "ru", sources["synonyms"], sources["study"])
        self.assertEqual(card["german"], "Tisch")
        self.assertEqual(card["study"]["state"], "queue_learning")

    def test_an_unknown_id_yields_nothing(self):
        self.assertIsNone(load_card_sources(self.db, USER, "no-such-word"))

    def test_the_full_row_carries_the_card_only_columns(self):
        # get_word_by_id omits these; the card would silently lose the adjective
        # degrees and both conjugation blocks.
        word = add_word(
            self.db,
            {
                "id": "unique",
                "type": "adjektiv",
                "german": "schnell",
                "komparativ": "schneller",
                "superlativ": "am schnellsten",
                "russian": "быстрый",
                "topic": "1",
            },
        )
        stored = get_word_full(self.db, word["id"])
        self.assertEqual(stored["komparativ"], "schneller")
        self.assertIn("conjugation_praesens", stored)


if __name__ == "__main__":
    unittest.main()
