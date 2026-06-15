"""Decide whether a candidate listing is the *same product* as a market watch.

The Browse API keyword search is broad: a query like "dyson airblade hu02" also
returns wall brackets, replacement filters, spare parts, faulty units, and
multi-item lots. Pricing the market over that mix produces a meaningless median,
so before computing anything we narrow the sample to genuinely comparable
listings with a few high-signal, dependency-free heuristics:

  * Model numbers — alphanumeric codes like ``hu02`` or ``g991b`` are the
    strongest single signal of a specific product. If the query carries one, a
    comparable title must carry it too. This alone rejects HU01/HU03 variants
    and most accessories.
  * Accessory / parts / lot / damage vocabulary — a title that *introduces*
    these words (when the query did not) is almost never the item itself.
  * Content coverage — the brand and key nouns from the query must mostly appear
    in the title, catching unrelated results the keyword search slipped in.

It is deliberately rule-based rather than ML: fast enough to run over every
listing on every poll, explainable, and dependency-free. Precision is favoured
over recall — it is better to drop a borderline listing than to poison the
median with a non-comparable one; the service guards against over-filtering with
a minimum-sample gate and surfaces the comparable count so a watch can be tuned.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from . import semantic

try:  # rapidfuzz is a light core dependency; degrade gracefully if absent.
    from rapidfuzz import fuzz as _fuzz

    def fuzzy_ratio(a: str, b: str) -> float:
        return _fuzz.token_set_ratio(a, b) / 100.0
except Exception:  # pragma: no cover - only when rapidfuzz missing

    def fuzzy_ratio(a: str, b: str) -> float:
        return 0.0


T = TypeVar("T")

# Words that carry no identifying signal and must not count toward matching.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "with", "in", "on", "to", "by",
    "new", "brand", "genuine", "official", "original", "authentic", "boxed",
    "bnib", "bnwt", "uk", "eu", "us", "free", "fast", "postage", "delivery",
    "ship", "shipping", "sealed", "unused", "mint", "condition", "item", "items",
}

# A title that introduces any of these single words (when the query did not) is
# almost always an accessory, a spare, a damaged unit, or a multi-item lot.
# Curated to be high-signal: ambiguous words ("for", "box", "set", "screen")
# are deliberately omitted to avoid dropping genuine comparables.
ACCESSORY_TOKENS = {
    "bracket", "filter", "cartridge", "charger", "adapter", "adaptor", "psu",
    "replacement", "spare", "spares", "repair", "repairs", "manual", "strap",
    "mount", "holder", "decal", "sticker", "skin", "protector", "grip", "lead",
    "leads", "nozzle", "hose", "cover", "case", "cable", "stylus", "lanyard",
}
DAMAGE_TOKENS = {"faulty", "broken", "damaged", "cracked", "untested", "incomplete"}
LOT_TOKENS = {
    "lot", "lots", "joblot", "joblots", "bundle", "bundles", "wholesale",
    "x2", "x3", "x4", "x5", "x6", "x8", "x10", "x12",
}
EXCLUSION_TOKENS = ACCESSORY_TOKENS | DAMAGE_TOKENS | LOT_TOKENS

# Lot/bundle quantity patterns, matched on the raw lowercased title so "x5",
# "5x", "lot of 5", "bundle of 5", "5-pack" etc. are recognised. Only fires with
# an explicit lot/multi signal, so single items ("iPhone 13") never match.
_LOT_QTY_RES = [
    re.compile(r"(?:job\s*lot|joblot|lot|bundle|pack|set)\s*of\s*(\d{1,3})"),
    re.compile(r"(\d{1,3})\s*(?:x\b|pcs|pc\b|pieces|units|[- ]?pack)"),
    re.compile(r"\bx\s*(\d{1,3})\b"),
]

def lot_quantity(title: str) -> int | None:
    """Number of units in a lot/bundle title, or None if it isn't a countable lot."""
    low = (title or "").lower()
    if " pair" in f" {low}":
        return 2
    quantities = []
    for pattern in _LOT_QTY_RES:
        for match in pattern.finditer(low):
            try:
                n = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 2 <= n <= 200:
                quantities.append(n)
    return max(quantities) if quantities else None


# Multi-word signals matched as whole phrases in the normalised title.
EXCLUSION_PHRASES = {
    "for parts", "for spares", "spares or repair", "spares or repairs",
    "or repair", "not working", "does not work", "not tested", "no power",
    "box only", "empty box", "read description", "sold as seen", "job lot",
}

# Model-line qualifiers. These distinguish *different products* at different
# prices ("iphone 13" vs "iphone 13 pro max"), so a qualifier the query did not
# ask for must not appear in a comparable, and one it did ask for is required.
QUALIFIERS = {
    "pro", "max", "mini", "plus", "ultra", "se", "air", "lite", "digital",
    "slim", "premium", "deluxe",
}
# Reading order for a multi-qualifier label, e.g. {"pro", "max"} -> "pro max".
QUALIFIER_ORDER = [
    "pro", "max", "plus", "ultra", "mini", "se", "air", "lite", "digital",
    "slim", "premium", "deluxe",
]

# Variant attributes — same product, different flavour. The query pins them when
# present; when absent, the service prices each variant separately instead of
# blending them into one meaningless median. Ambiguous words that double as a
# condition or filler ("mint", "clear", "natural", "space", "sand") are omitted.
COLOURS = {
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
    "pink", "grey", "gray", "silver", "gold", "bronze", "brown", "beige",
    "navy", "teal", "maroon", "olive", "indigo", "violet", "rose", "graphite",
    "charcoal", "midnight", "starlight", "titanium", "champagne", "ivory",
    "cream", "burgundy", "lavender", "turquoise", "gunmetal",
}
# Spelling variants that name the same colour. Folded to one canonical value so a
# UK "grey" query still matches a US "gray" listing (and vice versa) instead of
# silently dropping the comparable from the price sample.
_COLOUR_ALIASES = {"gray": "grey"}


def canonical_colours(tokens: set[str]) -> set[str]:
    """Colour tokens from ``tokens``, folding spelling synonyms (e.g. gray→grey)."""
    return {_COLOUR_ALIASES.get(token, token) for token in tokens if token in COLOURS}


_CAPACITY_RE = re.compile(r"(\d+)\s*(gb|tb)\b")

# Numbers that are measurements/specs, not model identifiers.
_UNIT_RE = re.compile(
    r"^\d+("
    r"v|w|kw|hz|khz|mhz|ghz|gb|tb|mb|kb|mm|cm|m|ml|l|kg|g|mg|mah|wh|k|mp|"
    r"in|ft|oz|lb|pc|pcs|st|pk|amp|a"
    r")$"
)
_WORD_RE = re.compile(r"[a-z0-9]+")

DEFAULT_COVERAGE = 0.6


def normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


# Common short-hands mapped to a canonical phrase so a "ps5" watch matches
# "PlayStation 5" titles and vice versa. Extensible at runtime via the ALIASES
# environment variable (see register_aliases).
DEFAULT_ALIASES = {
    "ps5": "playstation 5",
    "ps4": "playstation 4",
    "ps3": "playstation 3",
    "series x": "xbox series x",
    "series s": "xbox series s",
    "air pods": "airpods",
    "mac book": "macbook",
    "i phone": "iphone",
}
_ALIASES = dict(DEFAULT_ALIASES)


def register_aliases(pairs: Iterable[tuple[str, str]]) -> None:
    for variant, canonical in pairs:
        v = " ".join(tokenize(variant))
        c = " ".join(tokenize(canonical))
        if v and c:
            _ALIASES[v] = c


def canonicalize(text: str) -> str:
    """Normalise text and fold known short-hands to their canonical phrase."""
    norm = " ".join(tokenize(text))
    for variant in sorted(_ALIASES, key=len, reverse=True):
        norm = re.sub(rf"\b{re.escape(variant)}\b", _ALIASES[variant], norm)
    return norm


def model_numbers(text: str) -> set[str]:
    """Alphanumeric model codes: contain both a letter and a digit, not a unit."""
    models = set()
    for token in tokenize(text):
        if len(token) < 2:
            continue
        if not (any(c.isalpha() for c in token) and any(c.isdigit() for c in token)):
            continue
        if _UNIT_RE.match(token):
            continue
        models.add(token)
    return models


def content_tokens(text: str) -> set[str]:
    """Identifying tokens: drop stopwords and single characters."""
    return {token for token in tokenize(text) if token not in STOPWORDS and len(token) >= 2}


# Measurement/storage units handled elsewhere (capacity rule) or that are
# incidental specs rather than the model identifier.
_DISCRIM_SKIP_RE = re.compile(
    r"^\d+(v|hz|khz|mhz|ghz|w|kw|gb|tb|mb|mm|cm|m|inch|in|ft|hp|ml|l|kg|g|mg|mah|wh|k|mp)$"
)


def discriminators(text: str) -> set[str]:
    """Digit-bearing tokens that pin product identity (13, hu02, g991b, qe55).

    These are the exact discriminators between near-identical products (iPhone 13
    vs 14, PS4 vs PS5), so they are required verbatim — the fuzzy and semantic
    layers may relax *words*, never these. Pure measurement/storage units are
    excluded (capacity is enforced separately).
    """
    out = set()
    for token in tokenize(text):
        if not any(ch.isdigit() for ch in token):
            continue
        if token in STOPWORDS or _DISCRIM_SKIP_RE.match(token):
            continue
        out.add(token)
    return out


def _singular(token: str) -> str:
    return token[:-1] if token.endswith("s") and len(token) > 3 else token


def _qualifier_label(qualifiers: set[str]) -> str:
    """Canonical label for a model line: "base", "pro", "pro max", …"""
    if not qualifiers:
        return "base"
    order = {name: i for i, name in enumerate(QUALIFIER_ORDER)}
    return " ".join(sorted(qualifiers, key=lambda q: (order.get(q, len(order)), q)))


def attributes(text: str) -> dict[str, object]:
    """Extract variant attributes from a title or query.

    Returns ``capacity`` / ``colour`` as a single canonical value (or None when
    absent/ambiguous) and ``qualifier`` as a model-line label that is always
    present ("base" when none) — these drive clustering. Also returns the raw
    sets for comparability checks.
    """
    capacities = {f"{int(m.group(1))}{m.group(2)}" for m in _CAPACITY_RE.finditer(text.lower())}
    token_set = set(tokenize(text))
    colours = canonical_colours(token_set)
    qualifiers = token_set & QUALIFIERS
    return {
        "capacity": next(iter(capacities)) if len(capacities) == 1 else None,
        "colour": ",".join(sorted(colours)) if colours else None,
        "qualifier": _qualifier_label(qualifiers),
        "capacities": capacities,
        "colours": colours,
        "qualifiers": qualifiers,
    }


def normalize_capacity(value: str) -> str | None:
    """Canonicalise a capacity string ("256 GB", "256GB") to "256gb"."""
    match = _CAPACITY_RE.search((value or "").lower())
    return f"{int(match.group(1))}{match.group(2)}" if match else None


def specified_dimensions(query: str) -> set[str]:
    """Variant dimensions the query already pins (so they need not be clustered)."""
    attrs = attributes(query)
    dims = {dim for dim in ("capacity", "colour") if attrs[dim]}
    if attrs["qualifiers"]:
        dims.add("qualifier")
    return dims


def is_comparable(
    query: str,
    title: str,
    *,
    extra_excludes: Iterable[str] = (),
    coverage: float = DEFAULT_COVERAGE,
    fuzzy_threshold: float | None = None,
    semantic_ok: bool = False,
    allow_lots: bool = False,
) -> bool:
    """Whether ``title`` is the same product as ``query``.

    Hard guardrails (exclusions, identity discriminators, pinned attributes) can
    never be overridden. The final relevance test — does the title describe the
    same thing — passes on *any* of token coverage, fuzzy ratio, or semantic
    similarity, so reworded titles match while the guardrails keep precision.
    """
    query = canonicalize(query)
    title = canonicalize(title)
    q_norm = normalize(query)
    t_norm = normalize(title)
    q_tokens = set(q_norm.split())
    t_tokens = set(t_norm.split())

    # 1) Phrase exclusions the query did not ask for.
    padded_title = f" {t_norm} "
    phrases = EXCLUSION_PHRASES - {"job lot"} if allow_lots else EXCLUSION_PHRASES
    for phrase in phrases:
        if phrase not in q_norm and f" {phrase} " in padded_title:
            return False

    # 2) Single-word exclusions introduced by the title (lot terms kept when the
    #    caller is hunting lot/bundle arbitrage).
    base_excludes = (ACCESSORY_TOKENS | DAMAGE_TOKENS) if allow_lots else EXCLUSION_TOKENS
    token_excludes = set(base_excludes)
    for term in extra_excludes:
        token_excludes.update(tokenize(term))
    for token in t_tokens - q_tokens:
        if token in token_excludes or _singular(token) in token_excludes:
            return False

    # 3) Identity discriminators (model numbers / salient digits) are pinned
    #    exactly — this is what keeps iPhone 13 ≠ 14 and PS4 ≠ PS5 even when the
    #    fuzzy/semantic layers consider them similar.
    if not discriminators(query) <= t_tokens:
        return False

    # 4) A model-line qualifier the query *names* is pinned exactly.
    q_attrs = attributes(query)
    t_attrs = attributes(title)
    if q_attrs["qualifiers"] and q_attrs["qualifiers"] != t_attrs["qualifiers"]:
        return False

    # 5) Variant attributes the query pinned (a specific capacity or colour) must
    #    be present; unspecified ones are left for the pricing stage to cluster.
    if q_attrs["capacities"] and not q_attrs["capacities"] <= t_attrs["capacities"]:  # type: ignore[operator]
        return False
    if q_attrs["colours"] and not q_attrs["colours"] <= t_attrs["colours"]:  # type: ignore[operator]
        return False

    # 6) Relevance: brand/key nouns present (coverage), OR a high fuzzy ratio
    #    (word order / typos), OR semantic similarity (reworded / cross-category).
    q_content = content_tokens(query)
    if not q_content:
        return True
    if len(q_content & t_tokens) / len(q_content) >= coverage:
        return True
    if fuzzy_threshold is not None and fuzzy_ratio(q_norm, t_norm) >= fuzzy_threshold:
        return True
    return semantic_ok


def filter_comparable(
    query: str,
    items: Sequence[T],
    *,
    key: Callable[[T], str] = lambda item: item.title,  # type: ignore[attr-defined]
    extra_excludes: Iterable[str] = (),
    coverage: float = DEFAULT_COVERAGE,
    fuzzy_threshold: float | None = None,
    semantic_threshold: float | None = None,
    allow_lots: bool = False,
) -> list[T]:
    """Keep only the items whose title is comparable to ``query``.

    Semantic similarity (when enabled and available) is computed once for the
    whole batch and folded into each item's relevance test.
    """
    excludes = list(extra_excludes)
    semantic_ok = [False] * len(items)
    if semantic_threshold is not None:
        sims = semantic.similarities(canonicalize(query), [canonicalize(key(i)) for i in items])
        if sims is not None:
            semantic_ok = [s >= semantic_threshold for s in sims]
    return [
        item
        for item, sok in zip(items, semantic_ok)
        if is_comparable(
            query,
            key(item),
            extra_excludes=excludes,
            coverage=coverage,
            fuzzy_threshold=fuzzy_threshold,
            semantic_ok=sok,
            allow_lots=allow_lots,
        )
    ]
