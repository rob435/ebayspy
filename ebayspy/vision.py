"""Optional image verification + condition read via CLIP, with graceful fallback.

Uses fastembed's CLIP (ONNX, no torch) so it can run on a small server. CLIP
embeds text and images into the *same* space, which lets us, with no reference
image:
  * verify a listing photo actually depicts the product (text↔image similarity);
  * read condition zero-shot (compare the photo to a few condition prompts) to
    catch mistitled/misgraded gems — e.g. a listing tagged "for parts" whose
    photo clearly shows a pristine, boxed unit.

With a *reference* stock image of the item it also does the most direct check of
all — image↔image: does this listing's photo actually show the same product the
reference does? (:func:`image_match`, :func:`pick_reference`.)

Speed: every listing image is downloaded and embedded at most once — vectors are
memoised by URL (:data:`_vector_cache`), so the per-deal text-match, vision flags
and reference comparison all share a single embed, and a listing seen again on a
later poll is free. Call :func:`preload` once at startup (off the event loop) so
the model load/download never blocks the first deal check.

Everything is best-effort: if fastembed/Pillow or the models are unavailable, the
functions return None and the rest of the pipeline carries on without vision.
"""

from __future__ import annotations

import collections
import io
import logging
from collections.abc import Sequence

log = logging.getLogger(__name__)

# Listing-image embeddings memoised by URL (L2-normalised vectors). Bounded LRU so
# a long-running watcher can't grow it without limit; eBay image URLs are stable
# per item, so a cache hit is always the right vector.
_vector_cache: collections.OrderedDict[str, object] = collections.OrderedDict()
_CACHE_CAP = 512

TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"

# Zero-shot condition prompts → label.
CONDITION_PROMPTS = {
    "new": "a brand new product, sealed and boxed in original packaging",
    "used": "a used second-hand product in good cosmetic condition",
    "broken": "a broken, cracked, damaged or for-parts product",
    "accessory": "only a box, packaging, cables or accessories, without the main product",
}

# A clean studio render vs a real user snapshot — a dropship/scam tell on "used" listings.
STOCK_PROMPTS = {
    "stock": "a clean studio product render or official catalog photo on a plain white background",
    "real": "a real snapshot taken by a person, the item on a table, floor or carpet,"
    " with background clutter and uneven lighting",
}

# Visible damage vs pristine cosmetic condition.
DAMAGE_PROMPTS = {
    "damaged": "a cracked, scratched, dented, chipped or visibly damaged product",
    "pristine": "a flawless product in pristine cosmetic condition with no visible damage",
}

# A single unit vs a multi-item bundle/lot, for lot detection.
COUNT_PROMPTS = {
    "single": "a photo of a single item",
    "multiple": "a photo of several items grouped together, a bundle or lot of multiple units",
}

# Stated conditions that, if the photo looks new, signal a possible underpriced gem.
_POOR_TERMS = ("part", "spares", "repair", "faulty", "broken", "not working", "damaged")

_text_model = None
_image_model = None
_condition_vectors: dict[str, object] | None = None
_stock_vectors: dict[str, object] | None = None
_damage_vectors: dict[str, object] | None = None
_count_vectors: dict[str, object] | None = None
_client = None
_loaded = False
_disabled = False


def disable() -> None:
    global _disabled
    _disabled = True


def preload() -> bool:
    """Eagerly load the CLIP models + prompt vectors so the first deal check pays
    no load/download cost inline. Call once at startup from a background thread
    (e.g. ``asyncio.to_thread(vision.preload)``); it never raises and is a no-op
    when vision is disabled or already loaded. Returns True when vision is ready."""
    return _load()


def _load() -> bool:
    global _text_model, _image_model, _client, _loaded
    global _condition_vectors, _stock_vectors, _damage_vectors, _count_vectors
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

        def embed(prompts: dict[str, str]) -> dict[str, object]:
            vectors = _text_model.embed(list(prompts.values()))  # type: ignore[union-attr]
            return {
                label: vec / (np.linalg.norm(vec) + 1e-9)
                for label, vec in zip(prompts, vectors)
            }

        _condition_vectors = embed(CONDITION_PROMPTS)
        _stock_vectors = embed(STOCK_PROMPTS)
        _damage_vectors = embed(DAMAGE_PROMPTS)
        _count_vectors = embed(COUNT_PROMPTS)
        log.info("Vision matching enabled (fastembed CLIP)")
    except Exception:
        log.info(
            "Vision matching unavailable (install the 'vision' extra); skipping image checks.",
            exc_info=True,
        )
        # Reset every cache so a partial failure can't leave stale vectors behind.
        _text_model = None
        _condition_vectors = _stock_vectors = _damage_vectors = _count_vectors = None
    return _text_model is not None


def available() -> bool:
    return _load()


def _image_vector(image_url: str):
    """Download + embed (and L2-normalise) a listing image, memoised by URL.

    Raises on a failed download/decode so callers can fall back; only successful
    vectors are cached, so the same image is fetched and embedded at most once
    across the text-match, vision flags and reference comparison — and across
    polls."""
    cached = _vector_cache.get(image_url)
    if cached is not None:
        _vector_cache.move_to_end(image_url)
        return cached
    import numpy as np

    response = _client.get(image_url)  # type: ignore[union-attr]
    response.raise_for_status()
    from PIL import Image

    image = Image.open(io.BytesIO(response.content)).convert("RGB")
    vec = next(iter(_image_model.embed([image])))  # type: ignore[union-attr]
    vec = vec / (np.linalg.norm(vec) + 1e-9)
    _vector_cache[image_url] = vec
    while len(_vector_cache) > _CACHE_CAP:
        _vector_cache.popitem(last=False)
    return vec


def _safe_image_vector(image_url: str):
    """:func:`_image_vector` that returns None instead of raising — for callers
    embedding several images where one bad URL shouldn't sink the batch."""
    if not image_url:
        return None
    try:
        return _image_vector(image_url)
    except Exception:
        log.debug("Vision embed failed for %s", image_url, exc_info=True)
        return None


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


def image_match(reference_url: str, image_url: str) -> float | None:
    """CLIP cosine similarity in [-1, 1] between a reference (stock) image of the
    item and a listing photo — the most direct check there is that the listing
    actually shows the same product. None when vision is unavailable or either
    image can't be embedded. Both vectors are cached, so re-checking a listing or
    reusing the reference across deals costs nothing."""
    if not _load() or not reference_url or not image_url:
        return None
    reference_vec = _safe_image_vector(reference_url)
    image_vec = _safe_image_vector(image_url)
    if reference_vec is None or image_vec is None:
        return None
    return float(reference_vec @ image_vec)


def pick_reference(image_urls: Sequence[str], *, sample: int = 8) -> str | None:
    """Choose a representative 'stock' image for a product from a set of listing
    photos: the medoid — the image most similar on average to all the others —
    so an odd-one-out (wrong item, accessory-only, a lot photo) can't become the
    reference. Embeds at most ``sample`` distinct URLs (cached). Returns the
    chosen URL, or None if vision is unavailable or nothing embeds."""
    if not _load():
        return None
    embedded: list[tuple[str, object]] = []
    for url in list(dict.fromkeys(u for u in image_urls if u))[: max(sample, 0)]:
        vec = _safe_image_vector(url)
        if vec is not None:
            embedded.append((url, vec))
    if len(embedded) < 2:
        return embedded[0][0] if embedded else None
    import numpy as np

    matrix = np.array([vec for _, vec in embedded])
    # Vectors are L2-normalised, so the Gram matrix is pairwise cosine. Zero the
    # diagonal and the medoid is the row with the highest similarity to the rest.
    sims = matrix @ matrix.T
    np.fill_diagonal(sims, 0.0)
    best = int(np.argmax(sims.sum(axis=1)))
    return embedded[best][0]


def _classify_against(image_vec, vectors: dict[str, object]) -> tuple[str, float]:
    scored = {label: float(image_vec @ vec) for label, vec in vectors.items()}
    label = max(scored, key=lambda k: scored[k])
    return label, scored[label]


def classify_condition(image_url: str) -> tuple[str, float] | None:
    """Zero-shot condition label for the listing image, or None if unavailable."""
    if not _load() or not image_url or _condition_vectors is None:
        return None
    try:
        return _classify_against(_image_vector(image_url), _condition_vectors)
    except Exception:
        log.debug("Vision condition read failed for %s", image_url, exc_info=True)
        return None


def is_stock_photo(image_url: str) -> tuple[str, float] | None:
    """('stock'|'real', score) for the listing image, or None if unavailable."""
    if not _load() or not image_url or _stock_vectors is None:
        return None
    try:
        return _classify_against(_image_vector(image_url), _stock_vectors)
    except Exception:
        log.debug("Vision stock-photo read failed for %s", image_url, exc_info=True)
        return None


def is_damaged(image_url: str) -> tuple[str, float] | None:
    """('damaged'|'pristine', score) for the listing image, or None if unavailable."""
    if not _load() or not image_url or _damage_vectors is None:
        return None
    try:
        return _classify_against(_image_vector(image_url), _damage_vectors)
    except Exception:
        log.debug("Vision damage read failed for %s", image_url, exc_info=True)
        return None


def item_count_hint(image_url: str) -> tuple[str, float] | None:
    """('single'|'multiple', score) for the listing image, or None if unavailable."""
    if not _load() or not image_url or _count_vectors is None:
        return None
    try:
        return _classify_against(_image_vector(image_url), _count_vectors)
    except Exception:
        log.debug("Vision count read failed for %s", image_url, exc_info=True)
        return None


def vision_flags(image_url: str, stated_condition: str = "") -> dict | None:
    """Run every classifier against ONE image embedding (avoids a 4× download +
    embed per deal). Returns per-check (label, score)|None plus the gem 'upgrade'
    bool, or None when vision is unavailable or the image can't be embedded."""
    if not _load() or not image_url or _condition_vectors is None:
        return None
    try:
        image_vec = _image_vector(image_url)
    except Exception:
        log.debug("Vision flags failed for %s", image_url, exc_info=True)
        return None
    condition = _classify_against(image_vec, _condition_vectors) if _condition_vectors else None
    return {
        "condition": condition,
        "upgrade": bool(condition and is_condition_upgrade(stated_condition, condition[0])),
        "stock": _classify_against(image_vec, _stock_vectors) if _stock_vectors else None,
        "damage": _classify_against(image_vec, _damage_vectors) if _damage_vectors else None,
        "count": _classify_against(image_vec, _count_vectors) if _count_vectors else None,
    }


def compose_note(
    flags: dict,
    stated_condition: str = "",
    *,
    stock_threshold: float,
    damage_threshold: float,
    count_hint: bool,
    count_threshold: float,
) -> str:
    """Combine vision_flags() results into one ' · '-joined note for a deal alert.

    Pure (no model calls) so it is unit-testable with crafted flag dicts."""
    notes: list[str] = []
    condition = flags.get("condition")
    if flags.get("upgrade"):
        notes.append(f"📸 Photo looks new despite “{stated_condition or 'poor'}” — possible gem")
    elif condition:
        notes.append(f"📸 Image looks {condition[0]}")
    stated = (stated_condition or "").lower()
    poor = "used" in stated or any(term in stated for term in _POOR_TERMS)
    stock = flags.get("stock")
    if stock and stock[0] == "stock" and stock[1] >= stock_threshold and poor:
        notes.append("🖼️ Stock photo on a used listing — possible dropship/scam")
    damage = flags.get("damage")
    if damage and damage[0] == "damaged" and damage[1] >= damage_threshold:
        notes.append("🩹 Photo shows possible damage/defect")
    count = flags.get("count")
    if count_hint and count and count[0] == "multiple" and count[1] >= count_threshold:
        notes.append("🔢 Photo looks like multiple items (possible lot)")
    return " · ".join(notes)


def is_condition_upgrade(stated_condition: str, vision_label: str) -> bool:
    """True when the photo looks new/boxed but the listing is tagged poorly —
    the classic mistitled/misgraded underpriced gem."""
    stated = (stated_condition or "").lower()
    poor = any(term in stated for term in _POOR_TERMS)
    return poor and vision_label == "new"
