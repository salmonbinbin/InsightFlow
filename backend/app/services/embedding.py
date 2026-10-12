"""Phase 2 — embedding service.

The service turns a ``semantic_text`` string into a fixed-length vector. It
is split into two halves:

* :class:`LocalHashEmbedder` — deterministic, hash-based embedding that does
  not call any external API. Used by default and by all unit tests so the
  pipeline can be exercised without a vendor account.
* :class:`RemoteEmbedder` — thin HTTP client for an OpenAI-style embeddings
  endpoint. Activated when ``EMBEDDING_API_KEY`` / ``EMBEDDING_BASE_URL`` /
  ``EMBEDDING_MODEL`` are configured. Missing remote configuration fails
  closed; local hashing is only available as local-hash-v1.

The runner is responsible for persisting :class:`Embedding` rows. It must
call :func:`ensure_embeddings` exactly once per ``(feedback_analysis_id,
embedding_model)`` pair: re-running the pipeline does **not** generate
duplicate vectors.

This module deliberately never logs API keys or accepts them as arguments;
configuration is read from :mod:`app.db.config`.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Iterable, Sequence
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.config import settings
from app.models import (
    AnalysisRun,
    Feedback,
    FeedbackAnalysis,
)
from app.models import (
    Embedding as EmbeddingModel,
)

logger = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


class EmbeddingConfigError(RuntimeError):
    """Raised when embedding configuration is missing or invalid."""


class EmbeddingDimensionMismatch(RuntimeError):
    """Raised when a stored vector's dimension disagrees with the configured model."""


@dataclass(frozen=True)
class EmbeddingRequest:
    """A single ``semantic_text → vector`` request."""

    feedback_analysis_id: str
    text: str


@dataclass(frozen=True)
class EmbeddingResult:
    feedback_analysis_id: str
    vector: list[float]


class Embedder:
    """Abstract interface so the runner can be tested with a stub."""

    model: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError


class LocalHashEmbedder(Embedder):
    """Deterministic embedding used in tests and when no remote model is set.

    The algorithm is intentionally simple: each token contributes its SHA-256
    hash to a fixed-size sign table. The output is L2-normalised so the dot
    product equals cosine similarity. The result has no statistical meaning
    — it exists to make the pipeline deterministic and exercise the
    downstream clustering code without external dependencies.
    """

    def __init__(self, model: str, dimension: int) -> None:
        if dimension <= 0:
            raise EmbeddingConfigError(f"dimension must be positive, got {dimension}")
        self.model = model
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_hash_embed(text, self.dimension) for text in texts]


def _hash_embed(text: str, dimension: int) -> list[float]:
    vector = [0.0] * dimension
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if (digest[4] & 1) else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        # Empty tokens map to a canonical zero vector; the caller is
        # responsible for keeping it away from cosine math.
        return vector
    return [x / norm for x in vector]


class RemoteEmbedder(Embedder):
    """Thin OpenAI-compatible embeddings client.

    The endpoint and key come from environment only. ``api_key`` is never
    stored on the instance, logged, or echoed in error messages.
    """

    def __init__(
        self,
        *,
        model: str,
        dimension: int,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise EmbeddingConfigError("EMBEDDING_API_KEY is not configured")
        if not base_url:
            raise EmbeddingConfigError("EMBEDDING_BASE_URL is not configured")
        self.model = model
        self.dimension = dimension
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RemoteEmbedder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self.model, "input": list(texts)}
        url = f"{self._base_url}/v1/embeddings"
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise EmbeddingTransportError("embedding request failed") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise EmbeddingTransportError(f"embedding transient HTTP {response.status_code}")
        if response.status_code == 401 or response.status_code == 403:
            raise EmbeddingAuthError(
                f"embedding authentication rejected (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise EmbeddingProtocolError(f"embedding client error (HTTP {response.status_code})")
        try:
            data = response.json()
        except Exception as exc:  # pragma: no cover - json failure path
            raise EmbeddingProtocolError("embedding returned non-JSON body") from exc
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbeddingProtocolError("embedding response shape mismatch")
        indexed = {}
        for item in items:
            if not isinstance(item, dict):
                raise EmbeddingProtocolError("embedding response item not object")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < len(texts) or index in indexed:
                raise EmbeddingProtocolError("invalid embedding response index")
            vec = item.get("embedding")
            if not isinstance(vec, list) or not all(
                type(x) in (int, float) and math.isfinite(float(x)) for x in vec
            ):
                raise EmbeddingProtocolError("embedding vector missing or invalid")
            validate_vector(vec, self.dimension)
            indexed[index] = [float(x) for x in vec]
        return [indexed[i] for i in range(len(texts))]


class EmbeddingTransportError(RuntimeError):
    """Network or 5xx / 429 — treated as retriable."""


class EmbeddingAuthError(RuntimeError):
    """Authentication rejection — terminal."""


class EmbeddingProtocolError(RuntimeError):
    """Unexpected response shape — terminal."""


def build_embedder() -> Embedder:
    """Return the configured embedder.

    Missing remote configuration is rejected. Only local-hash-v1 permits
    deterministic local test vectors.

    Configuration is read via :class:`app.db.config.Settings` so values
    placed in ``backend/.env`` (e.g. ``EMBEDDING_API_KEY=...``) are picked
    up the same way as the rest of the Phase 1/2 settings. The process
    environment still wins over the file because pydantic-settings applies
    env vars after the file load.
    """
    model = settings.embedding_model
    dimension = settings.embedding_dimension
    api_key = settings.embedding_api_key
    base_url = settings.embedding_base_url
    if api_key and base_url:
        return RemoteEmbedder(
            model=model,
            dimension=dimension,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=60.0,
        )
    if model != "local-hash-v1" or api_key or base_url:
        raise EmbeddingConfigError("remote embedding requires model, key and base URL")
    return LocalHashEmbedder(model=model, dimension=dimension)


def is_embedding_remote() -> bool:
    """Diagnostic: True if the remote embedder will be used."""
    return bool(settings.embedding_api_key and settings.embedding_base_url)


@dataclass
class EmbeddingStats:
    """Per-run metrics: how many calls / how long / how many retries."""

    calls: int = 0
    retries: int = 0
    failures: int = 0
    latency_ms: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "embedding_calls": self.calls,
            "embedding_retries": self.retries,
            "embedding_failures": self.failures,
            "embedding_latency_ms": self.latency_ms,
        }


def _existing_embedding(
    db: Session, feedback_analysis_id: UUID, model: str
) -> EmbeddingModel | None:
    return db.scalar(
        select(EmbeddingModel).where(
            EmbeddingModel.feedback_analysis_id == feedback_analysis_id,
            EmbeddingModel.embedding_model == model,
        )
    )


def ensure_embeddings(
    db: Session,
    run: AnalysisRun,
    items: Sequence[tuple[FeedbackAnalysis, Feedback]],
    *,
    embedder: Embedder | None = None,
    stats: EmbeddingStats | None = None,
    max_retries: int | None = None,
) -> list[EmbeddingModel]:
    """Idempotent embedding generator.

    For each ``FeedbackAnalysis`` row, return the :class:`EmbeddingModel` row
    that holds its vector. Already-embedded rows are returned as-is; only
    rows missing for the configured ``embedding_model`` are sent through the
    embedder.

    ``stats`` accumulates call counts / latency / failures across the run;
    the runner decides where to persist it (the analysis run row keeps the
    Phase 1 counters, Phase 2 counters live elsewhere).
    """
    if max_retries is None:
        max_retries = settings.embedding_max_retries

    embedder_obj = embedder or build_embedder()
    stats_obj = stats or EmbeddingStats()
    out: list[EmbeddingModel] = []
    todo: list[tuple[FeedbackAnalysis, Feedback, str]] = []
    for analysis, feedback in items:
        existing = _existing_embedding(db, analysis.id, embedder_obj.model)
        if existing is not None:
            _verify_dimension(existing, embedder_obj.dimension)
            out.append(existing)
            continue
        todo.append((analysis, feedback, build_semantic_text_for(feedback, analysis)))
    if todo:
        _embed_batch(
            db,
            run,
            todo,
            embedder_obj,
            stats_obj,
            max_retries=max_retries,
        )
        for analysis, _feedback, _text in todo:
            existing = _existing_embedding(db, analysis.id, embedder_obj.model)
            if existing is None:
                # Defensive: should not happen because _embed_batch commits.
                raise EmbeddingProtocolError("embedding result missing after persistence")
            _verify_dimension(existing, embedder_obj.dimension)
            out.append(existing)
    return out


def build_semantic_text_for(feedback: Feedback, analysis: FeedbackAnalysis) -> str:
    """Return the semantic_text for a feedback analysis, regenerating if absent."""
    if analysis.semantic_text:
        return analysis.semantic_text
    # The classification step populates semantic_text; a missing value here
    # means the analysis was written by an older code path or imported by a
    # fixture. Rebuild it deterministically.
    from app.services.semantic_text import build_semantic_text

    return build_semantic_text(
        feedback.content,
        module=analysis.module,
        intent=analysis.intent,
        pain_point=analysis.pain_point,
    )


def validate_vector(vector, dimension):
    if len(vector) != dimension:
        raise EmbeddingDimensionMismatch("embedding dimension mismatch")
    if not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise EmbeddingProtocolError("embedding vector must contain finite numbers")
    if not any(x != 0 for x in vector):
        raise EmbeddingProtocolError("zero vector has no cosine direction")


def _verify_dimension(existing: EmbeddingModel, expected: int) -> None:
    validate_vector(existing.embedding, expected)
    if existing.embedding_dimension != expected:
        raise EmbeddingDimensionMismatch(
            f"embedding {existing.id} dimension {existing.embedding_dimension} "
            f"!= configured {expected}"
        )


def _embed_batch(
    db: Session,
    run: AnalysisRun,
    todo: list[tuple[FeedbackAnalysis, Feedback, str]],
    embedder: Embedder,
    stats: EmbeddingStats,
    *,
    max_retries: int,
) -> None:
    """Embed the ``todo`` items in chunks.

    Each successful call commits its rows in its own transaction so a later
    call's failure does not roll back earlier successes.
    """
    chunk_size = settings.embedding_max_batch_size
    for chunk in _chunked(todo, chunk_size):
        attempt = 0
        while True:
            stats.calls += 1
            started = time.monotonic()
            try:
                vectors = embedder.embed([text for _, _, text in chunk])
                latency = int((time.monotonic() - started) * 1000)
                stats.latency_ms += latency
            except EmbeddingTransportError:
                latency = int((time.monotonic() - started) * 1000)
                stats.latency_ms += latency
                stats.failures += 1
                attempt += 1
                if attempt > max_retries:
                    raise
                stats.retries += 1
                time.sleep(0.2 * (2 ** (attempt - 1)))
                continue
            _persist_chunk(db, run, chunk, vectors, embedder)
            break


def _chunked(items: list, size: int) -> Iterable[list]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]


def _persist_chunk(
    db: Session,
    run: AnalysisRun,
    chunk: list[tuple[FeedbackAnalysis, Feedback, str]],
    vectors: list[list[float]],
    embedder: Embedder,
) -> None:
    if len(vectors) != len(chunk):
        raise EmbeddingProtocolError(f"vector count {len(vectors)} != batch size {len(chunk)}")
    for vector in vectors:
        validate_vector(vector, embedder.dimension)
    for (analysis, _feedback, _text), vector in zip(chunk, vectors, strict=True):
        if len(vector) != embedder.dimension:
            raise EmbeddingDimensionMismatch(
                f"vector length {len(vector)} != model dimension {embedder.dimension}"
            )
        try:
            _upsert_embedding(db, run, analysis, vector, embedder)
            db.commit()
        except IntegrityError:
            db.rollback()
            # Another worker may have inserted the same row concurrently;
            # treat as success and continue.
            existing = _existing_embedding(db, analysis.id, embedder.model)
            if existing is None:
                raise


def _upsert_embedding(
    db: Session,
    run: AnalysisRun,
    analysis: FeedbackAnalysis,
    vector: list[float],
    embedder: Embedder,
) -> None:
    row = EmbeddingModel(
        feedback_analysis_id=analysis.id,
        run_id=run.id,
        embedding_model=embedder.model,
        embedding_dimension=embedder.dimension,
        embedding=vector,
    )
    # Use PostgreSQL ``ON CONFLICT DO NOTHING`` semantics via dialect-aware
    # insert so a concurrent insert does not raise. SQLite falls back to a
    # plain insert.
    if db.get_bind().dialect.name == "postgresql":
        stmt = pg_insert(EmbeddingModel).values(
            feedback_analysis_id=analysis.id,
            run_id=run.id,
            embedding_model=embedder.model,
            embedding_dimension=embedder.dimension,
            embedding=vector,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["feedback_analysis_id", "embedding_model"]
        )
        db.execute(stmt)
    else:
        existing = _existing_embedding(db, analysis.id, embedder.model)
        if existing is not None:
            return
        db.add(row)


__all__ = [
    "Embedder",
    "EmbeddingConfigError",
    "EmbeddingDimensionMismatch",
    "EmbeddingProtocolError",
    "EmbeddingResult",
    "EmbeddingStats",
    "EmbeddingTransportError",
    "EmbeddingAuthError",
    "LocalHashEmbedder",
    "RemoteEmbedder",
    "build_embedder",
    "build_semantic_text_for",
    "ensure_embeddings",
    "is_embedding_remote",
]
