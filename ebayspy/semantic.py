"""Optional semantic similarity for matching, with graceful degradation.

Uses a small *static* embedding model (model2vec — no torch, no GPU, fast on
CPU) so that titles describing the same product in very different words still
match, across any category rather than only the ones with hand-written rules.

Everything here is best-effort: if the library or model is unavailable, every
function returns None and the rule + fuzzy layers carry matching on their own.
The service therefore never hard-depends on the model being installed.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Small, retrieval-tuned static model: ~30MB, loads once, embeds via lookup+pool.
MODEL_NAME = "minishlab/potion-retrieval-32M"

_model = None
_loaded = False
_disabled = False


def disable() -> None:
    """Force semantic matching off (e.g. via config)."""
    global _disabled
    _disabled = True


def _get_model():
    global _model, _loaded
    if _disabled:
        return None
    if _loaded:
        return _model
    _loaded = True
    try:
        from model2vec import StaticModel

        _model = StaticModel.from_pretrained(MODEL_NAME)
        log.info("Semantic matching enabled (model2vec %s)", MODEL_NAME)
    except Exception:
        log.info(
            "Semantic matching unavailable (install the 'nlp' extra); "
            "falling back to rules + fuzzy matching.",
            exc_info=True,
        )
        _model = None
    return _model


def available() -> bool:
    return _get_model() is not None


def similarities(query: str, titles: list[str]) -> list[float] | None:
    """Cosine similarity of ``query`` against each title, or None if unavailable."""
    model = _get_model()
    if model is None or not titles:
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
