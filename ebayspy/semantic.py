"""Optional semantic similarity for matching, with graceful degradation.

Uses a small *static* embedding model (model2vec — no torch, no GPU, fast on
CPU) so that titles describing the same product in very different words still
match, across any category rather than only the ones with hand-written rules.

Everything here is best-effort: if the library or model is unavailable, every
function returns None and the rule + fuzzy layers carry matching on their own.
The service therefore never hard-depends on the model being installed. A
missing library switches semantics off for the life of the process; any other
load failure (a network blip while fetching the model, say) is retried after
``RETRY_SECONDS`` instead of latching off until restart.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Small, retrieval-tuned static model: ~30MB, loads once, embeds via lookup+pool.
MODEL_NAME = "minishlab/potion-retrieval-32M"
# Cool-down before a failed (non-import) model load is attempted again.
RETRY_SECONDS = 30 * 60

_model = None
_disabled = False
_unavailable = False          # the library itself is missing: never retry
_failed_at: float | None = None


def disable() -> None:
    """Force semantic matching off (e.g. via config)."""
    global _disabled
    _disabled = True


def _get_model():
    global _model, _unavailable, _failed_at
    if _disabled or _unavailable:
        return None
    if _model is not None:
        return _model
    if _failed_at is not None and time.monotonic() - _failed_at < RETRY_SECONDS:
        return None
    try:
        from model2vec import StaticModel
    except ImportError:
        _unavailable = True
        log.info(
            "Semantic matching unavailable (install the 'nlp' extra); "
            "falling back to rules + fuzzy matching."
        )
        return None
    try:
        _model = StaticModel.from_pretrained(MODEL_NAME)
        _failed_at = None
        log.info("Semantic matching enabled (model2vec %s)", MODEL_NAME)
    except Exception:
        _failed_at = time.monotonic()
        log.warning(
            "Semantic model failed to load; retrying in %d minutes.",
            RETRY_SECONDS // 60,
            exc_info=True,
        )
    return _model


def available() -> bool:
    return _get_model() is not None


def similarities(query: str, titles: list[str]) -> list[float] | None:
    """Cosine similarity of ``query`` against each title, or None if unavailable."""
    if not titles:
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        import numpy as np

        vectors = model.encode([query, *titles])
        query_vec = vectors[0]
        others = vectors[1:]
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-9)
        others_norm = others / (np.linalg.norm(others, axis=1, keepdims=True) + 1e-9)
        return (others_norm @ query_norm).tolist()
    except Exception:
        log.debug("Semantic similarity failed", exc_info=True)
        return None
