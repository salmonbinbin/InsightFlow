"""Phase 2 — Agglomerative clustering on cosine distance.

The implementation is intentionally small:

* Inputs are a list of (analysis_id, vector) pairs and a cosine distance
  threshold in ``[0, 1]``. ``0`` means identical, ``1`` means orthogonal.
* Pairwise distances are computed once with a flat ``O(n²)`` loop and a
  union-find structure performs single/average/complete linkage merges.
* Items whose nearest neighbour is farther than the threshold form their own
  cluster (outliers are still saved so the topology is reproducible).
* The output is a list of clusters with members and their similarity scores
  (i.e. ``1 - cosine_distance``).

We do not depend on scikit-learn so the Phase 2 pipeline stays pure Python
and runs unmodified on SQLite unit tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence


class ClusteringConfigError(ValueError):
    """Raised when the clustering parameters are invalid."""


@dataclass(frozen=True)
class VectorPoint:
    analysis_id: str
    vector: tuple[float, ...]


@dataclass
class ClusterCandidate:
    """An algorithmic cluster candidate returned by :func:`agglomerative_cluster`."""

    members: list[str] = field(default_factory=list)
    similarities: dict[str, float] = field(default_factory=dict)

    def add(self, analysis_id: str, similarity: float) -> None:
        self.members.append(analysis_id)
        self.similarities[analysis_id] = similarity


_ALLOWED_LINKAGES = frozenset({"average", "complete", "single"})


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Return normalized cosine distance in [0, 2]; antipodal vectors have distance 2."""
    if len(a) != len(b):
        raise ClusteringConfigError(f"vector dimension mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    cos = dot / (math.sqrt(na) * math.sqrt(nb))
    # Clamp floating-point errors in cosine similarity to [-1, 1].
    if cos > 1.0:
        cos = 1.0
    elif cos < -1.0:
        cos = -1.0
    return 1.0 - cos


def agglomerative_cluster(
    points: Sequence[VectorPoint],
    *,
    distance_threshold: float,
    linkage: str = "average",
) -> list[ClusterCandidate]:
    """Run agglomerative clustering with cosine distance.

    The output preserves the input order: cluster members appear in the
    order they were first added (single-link / average-link tie-break is the
    insertion order).
    """
    if not points:
        return []
    if linkage not in _ALLOWED_LINKAGES:
        raise ClusteringConfigError(
            f"unsupported linkage {linkage!r}; expected one of {sorted(_ALLOWED_LINKAGES)}"
        )
    if not math.isfinite(distance_threshold) or distance_threshold < 0 or distance_threshold > 1:
        raise ClusteringConfigError(f"distance_threshold {distance_threshold} must be in [0, 1]")

    dimension = len(points[0].vector)
    if len({p.analysis_id for p in points}) != len(points):
        raise ClusteringConfigError("duplicate analysis IDs")
    for point in points:
        if len(point.vector) != dimension or not dimension:
            raise ClusteringConfigError("vector dimension mismatch")
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in point.vector):
            raise ClusteringConfigError("invalid vector")
        if not any(x != 0 for x in point.vector):
            raise ClusteringConfigError("zero vector has no cosine direction")
    # Pre-compute the full pairwise distance matrix once.
    n = len(points)
    distances: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = cosine_distance(points[i].vector, points[j].vector)
            distances[i][j] = d
            distances[j][i] = d

    # Union-find merges clusters while their linkage distance stays below
    # the threshold.
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    # Members of each current cluster (root → list of original indices).
    members: dict[int, list[int]] = {i: [i] for i in range(n)}

    def cluster_distance(ra: int, rb: int) -> float:
        items_a = members[ra]
        items_b = members[rb]
        if linkage == "single":
            return min(distances[i][j] for i in items_a for j in items_b)
        if linkage == "complete":
            return max(distances[i][j] for i in items_a for j in items_b)
        # average linkage
        total = 0.0
        count = 0
        for i in items_a:
            for j in items_b:
                if i == j:
                    continue
                total += distances[i][j]
                count += 1
        return total / count if count else 0.0

    roots = list(range(n))
    # Iteratively merge the closest pair whose distance < threshold.
    while True:
        best_d = math.inf
        best_pair: tuple[int, int] | None = None
        # Scan all pairs; O(k²) per round, fine for our dataset sizes.
        for i_idx in range(len(roots)):
            i = roots[i_idx]
            for j_idx in range(i_idx + 1, len(roots)):
                j = roots[j_idx]
                d = cluster_distance(i, j)
                if d < best_d:
                    best_d = d
                    best_pair = (i, j)
        if best_pair is None or best_d >= distance_threshold:
            break
        a, b = best_pair
        union(a, b)
        members[find(a)] = members[a] + members[b]
        new_root = find(a)
        roots = [r for r in roots if r not in (a, b)] + [new_root]

    # Convert to ClusterCandidate list. Singletons whose nearest neighbour
    # exceeded the threshold are kept as their own cluster so the topology
    # always covers every input point.
    out: list[ClusterCandidate] = []
    for root in sorted(set(find(i) for i in range(n))):
        cluster_members = members[root]
        cand = ClusterCandidate()
        for idx in cluster_members:
            if len(cluster_members) == 1:
                # Outlier / singleton — similarity is undefined; record 0.0
                # so the API contract stays uniform.
                cand.add(points[idx].analysis_id, 0.0)
                continue
            nearest_sim = max(
                1.0 - distances[idx][other] for other in cluster_members if other != idx
            )
            cand.add(points[idx].analysis_id, max(nearest_sim, 0.0))
        out.append(cand)
    return out


def cluster_statistics(
    candidate: ClusterCandidate,
    severities: dict[str, int],
    sentiments: dict[str, str],
    user_ids: dict[str, str | None],
) -> dict[str, float | int]:
    """Aggregate cluster stats. Pure function so it is trivial to test."""
    sever_values = [severities[m] for m in candidate.members if m in severities]
    avg_sev = (sum(sever_values) / len(sever_values)) if sever_values else None
    neg = sum(1 for m in candidate.members if sentiments.get(m) == "negative")
    neg_ratio = (neg / len(candidate.members)) if candidate.members else 0.0
    user_set = {user_ids[m] for m in candidate.members if user_ids.get(m) is not None}
    return {
        "feedback_count": len(candidate.members),
        "unique_user_count": len(user_set),
        "average_severity": avg_sev,
        "negative_ratio": neg_ratio,
    }


__all__ = [
    "ClusterCandidate",
    "ClusteringConfigError",
    "VectorPoint",
    "agglomerative_cluster",
    "cluster_statistics",
    "cosine_distance",
]
