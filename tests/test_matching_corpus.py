"""Score the matcher on hand-labelled corpora and hold it above accuracy floors.

* ``match_corpus.json`` — synthetic titles across 22 queries, labelled under the
  "single, complete, working unit" contract (see ebayspy/matching.py).
* ``real_listings.json`` — real eBay titles (April-2021 crawl, 15 queries),
  labelled by buyer judgement independently of any matcher.

Rows labelled "ambiguous" are excluded. Floors sit a little below the measured
scores so an honest change does not trip on a single row; on failure the
confusion counts and every error (with its verdict reason) are printed.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from ebayspy.matching import match_verdict

DATA = Path(__file__).parent / "data"
PRODUCTION = {"coverage": 0.6, "fuzzy_threshold": 0.88}  # semantic off


def _score(name: str) -> tuple[Counter, list[str]]:
    rows = json.loads((DATA / name).read_text())
    counts: Counter = Counter()
    errors = []
    for row in rows:
        if row["match"] not in (True, False):
            continue
        verdict = match_verdict(row["query"], row["title"], **PRODUCTION)
        cell = ("tp" if verdict.ok else "fn") if row["match"] else ("fp" if verdict.ok else "tn")
        counts[cell] += 1
        if cell in ("fp", "fn"):
            errors.append(f"{cell} [{verdict.reason}] {row['query']!r} | {row['title']!r}")
    return counts, errors


@pytest.mark.parametrize(
    ("name", "min_precision", "min_recall"),
    [
        # measured: P 0.947 R 0.953 (161 tp, 9 fp, 8 fn, 243 tn)
        ("match_corpus.json", 0.93, 0.93),
        # measured: P 0.968 R 0.993 (995 tp, 33 fp, 7 fn, 395 tn)
        ("real_listings.json", 0.95, 0.97),
    ],
)
def test_corpus_accuracy_floor(name: str, min_precision: float, min_recall: float) -> None:
    counts, errors = _score(name)
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    summary = (
        f"{name}: tp={tp} fp={fp} fn={fn} tn={counts['tn']} "
        f"precision={precision:.3f} recall={recall:.3f}\n" + "\n".join(errors)
    )
    assert precision >= min_precision and recall >= min_recall, summary
