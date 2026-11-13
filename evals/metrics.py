"""Metric implementations, written out rather than imported.

Retrieval metrics are easy to get subtly wrong - nDCG with the wrong ideal
ranking, MRR counting from zero, Recall@k with a gold set larger than k - and
a quietly wrong metric is worse than no metric, because it still produces a
table. They are implemented here with their definitions stated, and unit
tested against hand-computed cases in ``tests/unit/test_metrics.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
def recall_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    """Fraction of gold items appearing in the top k.

    Note the denominator is ``min(len(gold), k)``, not ``len(gold)``. With 12
    gold items and k=5, recall against the full gold set is capped at 0.42 and
    no system can score higher, which makes the column meaningless. Capping
    the denominator asks the answerable question: of the gold items that could
    fit in k slots, how many did we get?
    """
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    hits = sum(1 for item in retrieved[:k] if item in gold_set)
    return hits / min(len(gold_set), k)


def precision_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    gold_set = set(gold)
    if not retrieved[:k]:
        return 0.0
    return sum(1 for item in retrieved[:k] if item in gold_set) / len(retrieved[:k])


def reciprocal_rank(retrieved: Sequence[str], gold: Iterable[str]) -> float:
    """1/rank of the first relevant item, ranks counted from 1."""
    gold_set = set(gold)
    for index, item in enumerate(retrieved, start=1):
        if item in gold_set:
            return 1.0 / index
    return 0.0


def dcg(relevances: Sequence[float]) -> float:
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(relevances))


def ndcg_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    """Binary-relevance nDCG@k with the ideal ranking capped at k."""
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    gains = [1.0 if item in gold_set else 0.0 for item in retrieved[:k]]
    ideal = [1.0] * min(len(gold_set), k)
    denominator = dcg(ideal)
    return dcg(gains) / denominator if denominator else 0.0


def hit_rate(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    return 1.0 if set(retrieved[:k]) & set(gold) else 0.0


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def confusion(predicted: Sequence[str], actual: Sequence[str], positive: str) -> dict[str, int]:
    tp = sum(1 for p, a in zip(predicted, actual, strict=True) if p == positive and a == positive)
    fp = sum(1 for p, a in zip(predicted, actual, strict=True) if p == positive and a != positive)
    fn = sum(1 for p, a in zip(predicted, actual, strict=True) if p != positive and a == positive)
    tn = sum(1 for p, a in zip(predicted, actual, strict=True) if p != positive and a != positive)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def precision_recall_f1(predicted: Sequence[str], actual: Sequence[str], positive: str) -> dict[str, float]:
    c = confusion(predicted, actual, positive)
    precision = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else 0.0
    recall = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        **c,
    }


def macro_f1(predicted: Sequence[str], actual: Sequence[str], labels: Iterable[str]) -> float:
    scores = [precision_recall_f1(predicted, actual, label)["f1"] for label in labels]
    return round(sum(scores) / len(scores), 4) if scores else 0.0


def accuracy(predicted: Sequence[str], actual: Sequence[str]) -> float:
    if not predicted:
        return 0.0
    return round(
        sum(1 for p, a in zip(predicted, actual, strict=True) if p == a) / len(predicted), 4
    )


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def cohens_kappa(rater_a: Sequence[Any], rater_b: Sequence[Any]) -> float:
    """Chance-corrected agreement.

    Reported instead of raw agreement because on a skewed label set two raters
    who both say "fine" most of the time agree 85% of the time by accident.
    Kappa says how much of that agreement is real.
    """
    if not rater_a or len(rater_a) != len(rater_b):
        return 0.0
    n = len(rater_a)
    labels = sorted(set(rater_a) | set(rater_b))
    observed = sum(1 for a, b in zip(rater_a, rater_b, strict=True) if a == b) / n
    expected = sum(
        (sum(1 for a in rater_a if a == label) / n) * (sum(1 for b in rater_b if b == label) / n)
        for label in labels
    )
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return round((observed - expected) / (1 - expected), 4)


def quadratic_weighted_kappa(rater_a: Sequence[int], rater_b: Sequence[int]) -> float:
    """For ordinal scores (1-5), where being off by one matters less than by four."""
    if not rater_a or len(rater_a) != len(rater_b):
        return 0.0
    labels = sorted(set(rater_a) | set(rater_b))
    index = {label: i for i, label in enumerate(labels)}
    size = len(labels)
    n = len(rater_a)

    observed = [[0.0] * size for _ in range(size)]
    for a, b in zip(rater_a, rater_b, strict=True):
        observed[index[a]][index[b]] += 1

    hist_a = [sum(1 for a in rater_a if a == label) for label in labels]
    hist_b = [sum(1 for b in rater_b if b == label) for label in labels]
    expected = [[hist_a[i] * hist_b[j] / n for j in range(size)] for i in range(size)]

    denominator = max((size - 1) ** 2, 1)
    num = sum(
        ((i - j) ** 2 / denominator) * observed[i][j] for i in range(size) for j in range(size)
    )
    den = sum(
        ((i - j) ** 2 / denominator) * expected[i][j] for i in range(size) for j in range(size)
    )
    return round(1 - num / den, 4) if den else 0.0


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------
def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1)))))
    return round(ordered[index], 4)


def summarise_latency(values: Sequence[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "mean_s": round(sum(values) / len(values), 4) if values else 0.0,
        "p50_s": percentile(values, 50),
        "p95_s": percentile(values, 95),
        "p99_s": percentile(values, 99),
        "max_s": round(max(values), 4) if values else 0.0,
    }


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Confidence interval for a rate. Reported so small-n results are not read as precise."""
    if trials == 0:
        return (0.0, 0.0)
    phat = successes / trials
    denominator = 1 + z**2 / trials
    centre = phat + z**2 / (2 * trials)
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * trials)) / trials)
    return (
        round(max(0.0, (centre - margin) / denominator), 4),
        round(min(1.0, (centre + margin) / denominator), 4),
    )


def two_proportion_z_test(s1: int, n1: int, s2: int, n2: int) -> dict[str, float]:
    """Is the difference between two rates real? Used by the A/B extension."""
    if n1 == 0 or n2 == 0:
        return {"z": 0.0, "p_value": 1.0, "significant": 0.0}
    p1, p2 = s1 / n1, s2 / n2
    pooled = (s1 + s2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return {"z": 0.0, "p_value": 1.0, "significant": 0.0}
    z = (p1 - p2) / se
    p_value = 2 * (1 - _normal_cdf(abs(z)))
    return {
        "rate_a": round(p1, 4),
        "rate_b": round(p2, 4),
        "z": round(z, 4),
        "p_value": round(p_value, 5),
        "significant": 1.0 if p_value < 0.05 else 0.0,
    }


def _normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
