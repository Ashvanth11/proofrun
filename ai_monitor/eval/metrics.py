"""Agreement metrics between two sets of scores.

Pure stdlib: the golden set is small enough that numpy/scipy would be a
dependency bought for four formulas.
"""

from statistics import mean
from typing import Optional

from pydantic import BaseModel

from ai_monitor.eval.golden_set import RELEVANT_THRESHOLD


class Agreement(BaseModel):
    n: int
    mae: float  # mean absolute error, the headline number
    rmse: float
    pearson_r: Optional[float]  # None when either side has no variance
    binary_agreement: float  # fraction agreeing on relevant vs not
    precision: Optional[float]
    recall: Optional[float]
    mean_bias: float  # positive = scores higher than reference


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    mx, my = mean(xs), mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    num = sum(a * b for a, b in zip(dx, dy))
    den = (sum(a * a for a in dx) * sum(b * b for b in dy)) ** 0.5
    if den == 0:
        return None
    return num / den


def compare(
    predicted: list[float],
    reference: list[float],
    threshold: float = RELEVANT_THRESHOLD,
) -> Agreement:
    """Compare predicted scores against reference (human) scores."""
    if len(predicted) != len(reference):
        raise ValueError("predicted and reference must be the same length")
    if not predicted:
        raise ValueError("no scores to compare")

    errors = [p - r for p, r in zip(predicted, reference)]

    pred_rel = [p >= threshold for p in predicted]
    ref_rel = [r >= threshold for r in reference]
    tp = sum(1 for p, r in zip(pred_rel, ref_rel) if p and r)
    fp = sum(1 for p, r in zip(pred_rel, ref_rel) if p and not r)
    fn = sum(1 for p, r in zip(pred_rel, ref_rel) if not p and r)

    return Agreement(
        n=len(predicted),
        mae=mean(abs(e) for e in errors),
        rmse=(mean(e * e for e in errors)) ** 0.5,
        pearson_r=pearson(predicted, reference),
        binary_agreement=mean(
            1.0 if p == r else 0.0 for p, r in zip(pred_rel, ref_rel)
        ),
        precision=tp / (tp + fp) if (tp + fp) else None,
        recall=tp / (tp + fn) if (tp + fn) else None,
        mean_bias=mean(errors),
    )


def calibration_buckets(
    predicted: list[float], reference: list[float], edges: Optional[list[float]] = None
) -> list[dict]:
    """Mean error within score bands, to locate *where* a scorer is miscalibrated.

    A uniform bias is a prompt-wide problem; a bias concentrated in one band
    (e.g. everything scored 0.6-0.8 should be lower) is a rubric problem.
    """
    edges = edges or [0.0, 0.3, 0.6, 0.9, 1.01]
    buckets = []
    for low, high in zip(edges, edges[1:]):
        pairs = [
            (p, r) for p, r in zip(predicted, reference) if low <= p < high
        ]
        if not pairs:
            buckets.append({"range": f"{low:.1f}-{high:.1f}", "n": 0})
            continue
        buckets.append(
            {
                "range": f"{low:.1f}-{high:.1f}",
                "n": len(pairs),
                "mean_predicted": mean(p for p, _ in pairs),
                "mean_reference": mean(r for _, r in pairs),
                "mean_bias": mean(p - r for p, r in pairs),
            }
        )
    return buckets
