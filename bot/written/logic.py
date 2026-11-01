"""Pure written-mode validation and the existing FSRS arithmetic."""

from ..storage import default_srs, update_fsrs

L1_DE = frozenset({"ru_de", "uk_de", "en_de"})
DE_L1 = frozenset({"de_ru", "de_uk", "de_en"})
MODES = L1_DE | DE_L1 | {"phrase_" + mode for mode in L1_DE | DE_L1} | {
    "pl_sg_ru", "pl_sg_uk", "pl_sg_en"}


def interval_divisor(mode):
    if mode not in MODES:
        raise ValueError("unsupported written mode")
    return 1.65 if mode.removeprefix("phrase_") in L1_DE else 2.0


def reduce_answer(before, kind, mode, today):
    if kind not in {"correct", "wrong", "idk"}:
        raise ValueError("unsupported written answer")
    state = dict(before) if before else default_srs(today)
    for _ in range(3 if kind == "idk" else 1):
        state = update_fsrs(state, 5 if kind == "correct" else 2,
                            today=today, interval_divisor=interval_divisor(mode))
    return state
