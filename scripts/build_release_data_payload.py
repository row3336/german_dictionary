"""Build the reviewed 2026-08-01 release-data payload from DB snapshots.

This is a development-time compiler, not a production migration. It starts with
the dictionary fields already applied on alpha, then overlays every owner-approved
exception. The output contains stable word IDs and exact old/new field values, so
the production backfill does not need access to the alpha database.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from scripts.reconcile_book_topics import DESCRIPTIONS, MERGES


TRANSFER_FIELDS = (
    "type",
    "article",
    "german",
    "plural",
    "infinitive",
    "prateritum",
    "partizip2",
    "russian",
    "ukrainian",
    "english",
    "komparativ",
    "superlativ",
    "example",
    "preposition_type",
    "case_type",
    "conjugation_praesens",
    "conjugation_praeteritum",
    "cefr_level",
)

NO_PLURAL = {
    "Wasser",
    "Quark",
    "Reis",
    "Geld",
    "Milch",
    "Osten",
    "Pech",
    "Sahne",
    "Butter",
    "Knoblauch",
    "Nähe",
    "Kindheit",
    "Vergangenheit",
    "Durst",
    "Geduld",
    "Hunger",
    "Mittagessen",
    "Schnee",
    "Süden",
    "Norden",
    "Westen",
    "Obst",
}

PLURALS = {
    "Eltern": "Eltern",
    "Geschwister": "Geschwister",
    "Jeans": "Jeans",
    "Mann": "Männer",
    "Schal": "Schals",
    "Wohnort": "Wohnorte",
    "Heimatland": "Heimatländer",
    "Pfund": "Pfund",
    "Block": "Blöcke, Blocks",
    "Karton": "Kartons, Kartone",
    "Kilo": "Kilo, Kilos",
    "Bank": "Banken",
    "Stift": "Stifte",
    "Bankkaufmann": "Bankkaufleute",
    "Wohnung": "Wohnungen",
    "Nacht": "Nächte",
    "Spaghetti": "Spaghetti",
    "Weihnachten": "Weihnachten",
    "Ostern": "Ostern",
    "Ski": "Ski, Skier",
    "Lebensmittel": "Lebensmittel",
    "Mittag": "Mittage",
    "Laden": "Läden",
    "Herbst": "Herbste",
    "Luftballon": "Luftballons",
    "Bundesland": "Bundesländer",
    "Land": "Länder",
    "Tuch": "Tücher",
    "Balkon": "Balkone",
    "Couch": "Couchs",
    "Kasten": "Kästen",
    "Arbeitszeit": "Arbeitszeiten, Arbeitzeiten",
    "Kolleg": "Kollegien, Kollegs",
    "Kollege": "Kollegen",
}

ARTICLES = {
    "Eltern": "die",
    "Geschwister": "die",
    "Jeans": "die",
    "Reis": "der",
    "Bank": "die",
    "Stift": "der",
    "Weihnachten": "das",
    "Ostern": "das",
    "Ski": "der",
    "Nähe": "die",
    "Lebensmittel": "das",
    "Mittag": "der",
    "Spaghetti": "die",
    "Ferien": "die",
    "Nebenkosten": "die",
    "Schichtdienst": "der",
}

PRAETERITUM_CONJUGATIONS = {
    "dürfen": ("durfte", "durftest", "durfte", "durften", "durftet", "durften"),
    "werden": ("wurde", "wurdest", "wurde", "wurden", "wurdet", "wurden"),
    "wollen": ("wollte", "wolltest", "wollte", "wollten", "wolltet", "wollten"),
    "können": ("konnte", "konntest", "konnte", "konnten", "konntet", "konnten"),
    "müssen": ("musste", "musstest", "musste", "mussten", "musstet", "mussten"),
    "mögen": ("mochte", "mochtest", "mochte", "mochten", "mochtet", "mochten"),
    "möchten": ("möchtete", "möchtetest", "möchtete", "möchteten", "möchtetet", "möchteten"),
}

PRAESENS_CONJUGATIONS = {
    "denken": ("denke", "denkst", "denkt", "denken", "denkt", "denken"),
    "kosten": ("koste", "kostest", "kostet", "kosten", "kostet", "kosten"),
    "spielen": ("spiele", "spielst", "spielt", "spielen", "spielt", "spielen"),
    "schwimmen": ("schwimme", "schwimmst", "schwimmt", "schwimmen", "schwimmt", "schwimmen"),
    "geboren werden": (
        "werde geboren",
        "wirst geboren",
        "wird geboren",
        "werden geboren",
        "werdet geboren",
        "werden geboren",
    ),
}

PREPOSITIONS = {
    "aus": ("local", "dativ", "из, с", "з, із, звідки, зсередини", "from, out of, out"),
    "bei": (
        "local",
        "dativ",
        "у, при, в, возле, среди",
        "у, біля, в, поряд, поряд з",
        "at, by, with, among, in, in the midst of",
    ),
    "bis": (
        "temporal",
        "akkusativ",
        "до, вплоть до",
        "до, аж до, до моменту, до часу",
        "until, up to, by, through",
    ),
    "in": ("local", "wechsel", "в, внутри, на", "у, в, на", "in, inside, into, within"),
    "nach": (
        "local",
        "dativ",
        "в направлении, в сторону, к",
        "у напрямку, в напрямку, до",
        "toward, towards, to",
    ),
    "seit": (
        "temporal",
        "dativ",
        "начиная с, с тех пор, в течение",
        "починаючи з, відтоді, протягом, від, з того часу",
        "since, during, for, for a period, from",
    ),
    "um": ("temporal", "akkusativ", "в", "о, об", "at"),
    "von": ("temporal", "dativ", "с, от", "з, від", "from"),
    "zu": (
        "local",
        "dativ",
        "к, в, в направлении к, на",
        "до, в, у напрямку до, в напрямку до",
        "to, in, in the direction of, at",
    ),
}


class PayloadBuildError(RuntimeError):
    """Raised when the source snapshots do not match the reviewed baseline."""


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_words(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(words)")}
        rows = {}
        for row in conn.execute("SELECT * FROM words ORDER BY id"):
            item = dict(row)
            for field in TRANSFER_FIELDS:
                item.setdefault(field, "")
            rows[item["id"]] = item
        return rows, columns
    finally:
        conn.close()


def _headword(row):
    return str(row.get("german") or row.get("infinitive") or "")


def _conjugation(forms):
    pronouns = ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie")
    return "\n".join(f"{pronoun} {form}" for pronoun, form in zip(pronouns, forms))


class DesiredWords:
    """Mutable reviewed result keyed by stable IDs, with strict headword lookup."""

    def __init__(self, prod, alpha):
        self.prod = prod
        self.rows = {
            word_id: {field: str(alpha[word_id].get(field) or "") for field in TRANSFER_FIELDS}
            for word_id in prod
        }
        self._by_prod_headword = {}
        for word_id, row in prod.items():
            self._by_prod_headword.setdefault(_headword(row).casefold(), []).append(word_id)

    def word_id(self, headword):
        matches = self._by_prod_headword.get(str(headword).casefold(), [])
        if len(matches) != 1:
            raise PayloadBuildError(f"expected one prod row for {headword!r}, found {matches}")
        return matches[0]

    def set(self, headword, **fields):
        word_id = self.word_id(headword)
        unknown = set(fields) - set(TRANSFER_FIELDS)
        if unknown:
            raise PayloadBuildError(f"unsupported fields for {headword}: {sorted(unknown)}")
        self.rows[word_id].update({key: str(value or "") for key, value in fields.items()})

    def preserve_prod(self, headword, fields):
        word_id = self.word_id(headword)
        self.rows[word_id].update({field: str(self.prod[word_id].get(field) or "") for field in fields})


def _apply_owner_decisions(desired):
    for headword in NO_PLURAL:
        desired.set(headword, plural="")
    for headword, plural in PLURALS.items():
        desired.set(headword, plural=plural)
    for headword, article in ARTICLES.items():
        desired.set(headword, article=article)

    desired.set("Flur", article="der", plural="Flure")
    desired.set("Kiwi", article="die", plural="Kiwis")
    desired.set("Model", german="Modell", article="das", plural="Modelle", cefr_level="B1")
    desired.set("Moment", article="der", plural="Momente")
    desired.set("Möbel", article="das", plural="Möbel, Möbeln")
    desired.set("Teil", article="der", plural="Teile")
    desired.set("Foto", article="das", plural="Fotos")
    desired.set("Gehalt", article="das", plural="Gehälter")
    desired.set("Joghurt", article="der, das, die", plural="Joghurts")
    desired.set("Paprika", article="die", plural="Paprika, Paprikas")
    desired.set("Kfz-mechaniker", german="Kfz-Mechaniker", article="der", plural="Kfz-Mechaniker")
    desired.set("Turnshuhe", german="Sneaker", article="der", plural="Sneakers", cefr_level="A2")
    desired.set("E-mail", german="E-Mail", plural="E-Mails")
    desired.set("Musli", german="Müsli", article="das", plural="Müslis", cefr_level="A1")

    unsafe_sense_fields = ("russian", "ukrainian", "english", "example")
    for headword in ("Flur", "Kiwi", "Model", "Moment", "Gehalt", "Paprika", "Teil"):
        desired.preserve_prod(headword, unsafe_sense_fields)
    desired.preserve_prod("Gummi", tuple(field for field in TRANSFER_FIELDS if field != "cefr_level"))
    desired.preserve_prod("Verwandte", ("russian", "ukrainian", "english"))

    desired.set("bitte", type="adverb", cefr_level="A1")
    desired.set("langsam", type="adjektiv", komparativ="langsamer", superlativ="am langsamsten", cefr_level="A1")
    desired.set("schnell", type="adjektiv", komparativ="schneller", superlativ="am schnellsten", cefr_level="A1")
    desired.set("viel", type="adjektiv", komparativ="mehr", superlativ="am meisten", cefr_level="A1")
    desired.set("selten", type="adjektiv", komparativ="seltener", superlativ="am seltensten", cefr_level="A2")
    desired.set("nur", type="partikel", cefr_level="A1")
    desired.set("Auf Wiedersehen", type="phrase", cefr_level="A1")
    desired.set("auf Wiederhören", type="phrase", cefr_level="A1")
    desired.set("ein bisschen", type="phrase", cefr_level="A1")
    desired.set("sehr gut", type="phrase", komparativ="", superlativ="", cefr_level="A1")
    for headword in ("ledig", "perfekt", "ganz", "lila", "rosa", "super"):
        desired.set(headword, komparativ="", superlativ="")

    desired.set(
        "abhängen",
        prateritum="hing ab",
        partizip2="abgehangen",
        russian=desired.prod[desired.word_id("abhängen")]["russian"],
        ukrainian=desired.prod[desired.word_id("abhängen")]["ukrainian"],
        english=desired.prod[desired.word_id("abhängen")]["english"],
        example=desired.prod[desired.word_id("abhängen")]["example"],
    )
    desired.set(
        "übernehmen",
        prateritum="übernahm",
        partizip2="übernommen",
        conjugation_praesens=_conjugation(("übernehme", "übernimmst", "übernimmt", "übernehmen", "übernehmt", "übernehmen")),
        russian=desired.prod[desired.word_id("übernehmen")]["russian"],
        ukrainian=desired.prod[desired.word_id("übernehmen")]["ukrainian"],
        example=desired.prod[desired.word_id("übernehmen")]["example"],
    )
    desired.set(
        "umgehen",
        prateritum="umging",
        partizip2="umgangen",
        conjugation_praesens=_conjugation(("umgehe", "umgehst", "umgeht", "umgehen", "umgeht", "umgehen")),
        russian=desired.prod[desired.word_id("umgehen")]["russian"],
        ukrainian=desired.prod[desired.word_id("umgehen")]["ukrainian"],
        english=desired.prod[desired.word_id("umgehen")]["english"],
        example=desired.prod[desired.word_id("umgehen")]["example"],
    )
    for verb, forms in PRAETERITUM_CONJUGATIONS.items():
        desired.set(verb, conjugation_praeteritum=_conjugation(forms))
    for verb, forms in PRAESENS_CONJUGATIONS.items():
        desired.set(verb, conjugation_praesens=_conjugation(forms))
    desired.set("möchten", prateritum="möchtete", partizip2="gemöchtet")
    desired.set("werden", partizip2="geworden")
    desired.set("senden", prateritum="sendete, sandte", partizip2="gesendet, gesandt")

    translations = {
        "aussteigen": {
            "russian": "выйти, сойти, выходить, высаживаться с транспорта",
            "ukrainian": "вийти, зійти, виходити, вийти з транспорту",
        },
        "Holz": {"russian": "дерево, древесина, (материал)", "ukrainian": "дерево, деревина, (матеріал)"},
        "anfangen": {"russian": "начинать, начать, приступать, приступить, разговорное"},
        "waschen": {
            "russian": "мыть, стирать, умываться, вымыть, мыться, очистить, застирать, (вещи)",
            "ukrainian": "мити, прати, очищати, обмивати, (речі)",
        },
        "Heizkörper": {
            "russian": "радиатор, батарея, батарея центрального отопления, нагревательный элемент",
            "ukrainian": "радіатор, батарея, опалювальний прилад",
        },
        "gucken": {
            "russian": "смотреть, глядеть, глянуть, поглядеть, посмотреть",
            "ukrainian": "дивитися, глянути, вдивлятися, переглядати, подивитися, придивлятися",
        },
        "joggen": {
            "russian": "бегать, бегать трусцой, совершать пробежку",
            "ukrainian": "бігати, бігти підтюпцем, здійснювати пробіжку",
        },
        "kochen": {
            "russian": "готовить, варить, кипеть, кипятить, вариться, приготовление",
            "ukrainian": "готувати, варити, готувати їжу, кип'ятити",
        },
        "Handy": {
            "russian": "мобильный телефон, сотовый телефон, мобильник",
            "ukrainian": "мобільний телефон, мобільник, стільниковий телефон",
        },
        "Spüle": {
            "russian": "раковина, мойка, умывальник, раковина на кухне",
            "ukrainian": "раковина, мийка, умивальник, кухня",
        },
    }
    for headword, fields in translations.items():
        desired.set(headword, **fields)
    desired.set("ausfallen", ukrainian=desired.prod[desired.word_id("ausfallen")]["ukrainian"])
    desired.set("übernehmen", ukrainian=desired.prod[desired.word_id("übernehmen")]["ukrainian"])

    regenschirm_id = desired.word_id("Regenschirm")
    existing_ru = desired.prod[regenschirm_id]["russian"].strip()
    desired.set("Regenschirm", russian=f"{existing_ru}, зонт от дождя" if existing_ru else "зонт от дождя")

    for headword, values in PREPOSITIONS.items():
        ptype, case, russian, ukrainian, english = values
        desired.set(
            headword,
            preposition_type=ptype,
            case_type=case,
            russian=russian,
            ukrainian=ukrainian,
            english=english,
        )

    verb_blank_levels = {
        "anwerfen": "A2",
        "geboren werden": "A1",
        "nachlesen": "A1",
        "reinkommen": "A2",
        "schwimmen": "A1",
    }
    for word_id, row in desired.rows.items():
        if row["cefr_level"]:
            continue
        word_type = row["type"].strip().lower()
        headword = _headword(row)
        if word_type in {"adjektiv", "adverb", "nomen", "partikel", "phrase", "pronomen"}:
            row["cefr_level"] = "A1"
        elif word_type == "verb" and headword in verb_blank_levels:
            row["cefr_level"] = verb_blank_levels[headword]
        else:
            raise PayloadBuildError(f"no approved CEFR rule for {word_id} {headword!r} ({word_type})")


def build_payload(prod_path, alpha_path):
    prod, prod_columns = _load_words(prod_path)
    alpha, _ = _load_words(alpha_path)
    if set(prod) != set(alpha):
        raise PayloadBuildError("prod and alpha stable word IDs differ")
    missing_baseline = {field for field in TRANSFER_FIELDS if field not in prod_columns}
    if missing_baseline - {"cefr_level"}:
        raise PayloadBuildError(f"prod snapshot misses unexpected fields: {sorted(missing_baseline)}")

    desired = DesiredWords(prod, alpha)
    _apply_owner_decisions(desired)

    patches = []
    for word_id in sorted(prod):
        fields = {}
        for field in TRANSFER_FIELDS:
            old = str(prod[word_id].get(field) or "")
            new = desired.rows[word_id][field]
            if old != new:
                fields[field] = {"old": old, "new": new}
        if fields:
            patches.append({"id": word_id, "label": _headword(desired.rows[word_id]), "fields": fields})

    synonym_pairs = []
    for left, right in (("Aufgabe", "Übung"), ("schauen", "gucken")):
        synonym_pairs.append(
            {
                "labels": [left, right],
                "word_ids": [desired.word_id(left), desired.word_id(right)],
            }
        )

    return {
        "format_version": 1,
        "release": "2026-08-01-prod-data",
        "source": {
            "prod_sha256": _sha256(prod_path),
            "alpha_sha256": _sha256(alpha_path),
            "word_count": len(prod),
        },
        "topics": {"merges": MERGES, "descriptions": DESCRIPTIONS},
        "synonym_pairs": synonym_pairs,
        "word_patches": patches,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod", required=True, help="Reviewed production DB snapshot")
    parser.add_argument("--alpha", required=True, help="Reviewed alpha DB snapshot")
    parser.add_argument("--output", required=True, help="Destination JSON payload")
    args = parser.parse_args()
    payload = build_payload(args.prod, args.alpha)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {output}: {len(payload['word_patches'])} words, "
        f"{sum(len(item['fields']) for item in payload['word_patches'])} field patches"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
