"""Optional image verification + condition read via CLIP, with graceful fallback.

Uses fastembed's CLIP (ONNX, no torch) so it can run on a small server. CLIP
embeds text and images into the *same* space, which lets us, with no reference
image:
  * verify a listing photo actually depicts the product (text↔image similarity);
  * read condition zero-shot (compare the photo to a few condition prompts) to
    catch mistitled/misgraded gems — e.g. a listing tagged "for parts" whose
    photo clearly shows a pristine, boxed unit.

Everything is best-effort: if fastembed/Pillow or the models are unavailable, the
functions return None and the rest of the pipeline carries on without vision.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"

# Zero-shot condition prompts → label.
CONDITION_PROMPTS = {
    "new": "a brand new product, sealed and boxed in original packaging",
    "used": "a used second-hand product in good cosmetic condition",
    "broken": "a broken, cracked, damaged or for-parts product",
    "accessory": "only a box, packaging, cables or accessories, without the main product",
}

# Stated conditions that, if the photo looks new, signal a possible underpriced gem.
_POOR_TERMS = ("part", "spares", "repair", "faulty", "broken", "not working", "damaged")

_text_model = None
_image_model = None
_condition_vectors: dict[str, object] | None = None
_client = None
_loaded = False
_disabled = False


def disable() -> None:
    global _disabled
    _disabled = True


def _load() -> bool:
    global _text_model, _image_model, _condition_vectors, _client, _loaded
    if _disabled:
        return False
    if _loaded:
        return _text_model is not None
    _loaded = True
    try:
        import httpx
        import numpy as np
        from fastembed import ImageEmbedding, TextEmbedding

        _text_model = TextEmbedding(TEXT_MODEL)
        _image_model = ImageEmbedding(IMAGE_MODEL)
        _client = httpx.Client(timeout=15, follow_redirects=True)
        vectors = list(_text_model.embed(list(CONDITION_PROMPTS.values())))
        _condition_vectors = {
            label: vec / (np.linalg.norm(vec) + 1e-9)
            for label, vec in zip(CONDITION_PROMPTS, vectors)
        }
        log.info("Vision matching enabled (fastembed CLIP)")
    except Exception:
        log.info(
            "Vision matching unavailable (install the 'vision' extra); skipping image checks.",
            exc_info=True,
        )
        _text_model = None
    return _text_model is not None


def available() -> bool:
    return _load()


def _image_vector(image_url: str):
    import numpy as np

    response = _client.get(image_url)  # type: ignore[union-attr]
    response.raise_for_status()
    from PIL import Image

    image = Image.open(__import__("io").BytesIO(response.content)).convert("RGB")
    vec = next(iter(_image_model.embed([image])))  # type: ignore[union-attr]
    return vec / (np.linalg.norm(vec) + 1e-9)


def match_score(image_url: str, query: str) -> float | None:
    """CLIP similarity between the listing image and the query text (does the
    photo look like a <query>?). None if vision is unavailable."""
    if not _load() or not image_url:
        return None
    try:
        import numpy as np

        image_vec = _image_vector(image_url)
        text_vec = next(iter(_text_model.embed([query])))  # type: ignore[union-attr]
        text_vec = text_vec / (np.linalg.norm(text_vec) + 1e-9)
        return float(image_vec @ text_vec)
    except Exception:
        log.debug("Vision match failed for %s", image_url, exc_info=True)
        return None


def classify_condition(image_url: str) -> tuple[str, float] | None:
    """Zero-shot condition label for the listing image, or None if unavailable."""
    if not _load() or not image_url or _condition_vectors is None:
        return None
    try:
        image_vec = _image_vector(image_url)
        scored = {label: float(image_vec @ vec) for label, vec in _condition_vectors.items()}
        label = max(scored, key=lambda k: scored[k])
        return label, scored[label]
    except Exception:
        log.debug("Vision condition read failed for %s", image_url, exc_info=True)
        return None


def is_condition_upgrade(stated_condition: str, vision_label: str) -> bool:
    """True when the photo looks new/boxed but the listing is tagged poorly —
    the classic mistitled/misgraded underpriced gem."""
    stated = (stated_condition or "").lower()
    poor = any(term in stated for term in _POOR_TERMS)
    return poor and vision_label == "new"
