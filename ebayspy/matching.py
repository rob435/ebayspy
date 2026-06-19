"""Decide whether a candidate listing is the *same product* as a market watch.

The Browse API keyword search is broad: a query like "dyson airblade hu02" also
returns wall brackets, replacement filters, spare parts, faulty units, and
multi-item lots. Pricing the market over that mix produces a meaningless median,
so before computing anything we narrow the sample to genuinely comparable
listings with a few high-signal, dependency-free heuristics:

  * Model numbers — alphanumeric codes like ``hu02`` or ``g991b`` are the
    strongest single signal of a specific product. If the query carries one, a
    comparable title must carry it too. This alone rejects HU01/HU03 variants
    and most accessories. Codes are read tolerant of how they are written —
    ``WH-1000XM5`` ≡ ``WH1000XM5`` — so a stray hyphen never drops a comparable.
  * Edition designators — the lone letter in *Xbox Series X* vs *Series S*, the
    *Mark II* / *mk2* in a camera body, and bare roman generations (*A7 III* vs
    *A7 II*) name different products that share every other word, so they are
    pinned exactly in both directions. Plain digits read like a human, too:
    ``2nd Gen`` ≡ ``2`` and ``S21+`` ≡ ``S21 Plus``.
  * Niche attributes — when the query names them, a graded-card grade
    (``PSA 10`` ≠ ``PSA 9``, and ``PSA10`` ≡ ``PSA 10``), a fragrance
    concentration (``EDP`` ≠ ``EDT``, ``Eau de Parfum`` ≡ ``EDP``), a liquid
    volume (``100ml`` ≠ ``60ml``), a long reference number with a sub-variant
    suffix (``116610`` ≡ ``116610LN``), and an aperture (``f/1.8`` ≡ ``f1.8``)
    are all read the way a collector would.
  * Accessory / parts / lot / damage vocabulary — a title that *introduces*
    these words (when the query did not) is almost never the item itself.
  * Content coverage — the brand and key nouns from the query must mostly appear
    in the title (plurals folded so ``games`` covers ``game``), catching
    unrelated results the keyword search slipped in.

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
    "slim", "premium", "deluxe", "oled",
    # Fragrance concentration — a genuinely different product at a different price
    # (EDP ≠ EDT ≠ EDC), so it pins when named and is priced per-cluster otherwise.
    "edp", "edt", "edc",
}
# Reading order for a multi-qualifier label, e.g. {"pro", "max"} -> "pro max".
QUALIFIER_ORDER = [
    "pro", "max", "plus", "ultra", "mini", "se", "air", "lite", "oled", "digital",
    "slim", "premium", "deluxe", "edp", "edt", "edc",
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
_HYPHEN_PAIR_RE = re.compile(r"([a-z0-9]+)-([a-z0-9]+)")
# "+" tacked onto a model name means the Plus model line (Galaxy S21+, Note 20+);
# tokenizing drops the symbol, so spell it out first so it matches "... Plus".
_PLUS_RE = re.compile(r"(?<=[a-z0-9])\+")
# Camera aperture: "f/1.8" and "f1.8" are the same lens — drop the slash so they
# tokenize alike (the decimal still splits, but identically on both sides).
_APERTURE_RE = re.compile(r"\bf\s*/\s*(\d)")
# Graded trading cards write the grade glued ("PSA10", "BGS9.5") or spaced
# ("PSA 10"); split the known grading prefixes so both forms land on "<co> <n>"
# and the grade number (the price driver) is pinned as a discriminator.
_GRADE_RE = re.compile(r"\b(psa|bgs|cgc|sgc)\s*(\d+(?:\.\d+)?)\b")

DEFAULT_COVERAGE = 0.6


def _join_model_hyphens(text: str) -> str:
    """Fold the hyphen out of a model code so its written forms unify.

    A model code is hyphenated inconsistently across listings — "WH-1000XM5",
    "WH1000XM5", "SM-G991B", "F-150" — and a human reads them all as one token.
    The digit is the tell: join the two sides only when one of them carries a
    number, so genuine hyphenated words ("t-shirt", "wi-fi", "blu-ray") are left
    intact. Applied to query and title alike, so every form lands on the same
    token and matching no longer hinges on a punctuation choice.
    """
    def repl(match: re.Match) -> str:
        left, right = match.group(1), match.group(2)
        if any(c.isdigit() for c in left) or any(c.isdigit() for c in right):
            return left + right
        return match.group(0)

    return _HYPHEN_PAIR_RE.sub(repl, text)


def normalize(text: str) -> str:
    return " ".join(tokenize(text))


def tokenize(text: str) -> list[str]:
    lowered = _PLUS_RE.sub(" plus", (text or "").lower())
    lowered = _APERTURE_RE.sub(r"f\1", lowered)
    lowered = _GRADE_RE.sub(r"\1 \2", lowered)
    return _WORD_RE.findall(_join_model_hyphens(lowered))


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
    # Fragrance concentration long-forms fold to their codes so "Eau de Parfum"
    # and "EDP" are one token (and distinct from EDT/EDC). Longest first.
    "eau de parfum": "edp",
    "eau de toilette": "edt",
    "eau de cologne": "edc",
}
_ALIASES = dict(DEFAULT_ALIASES)


def register_aliases(pairs: Iterable[tuple[str, str]]) -> None:
    for variant, canonical in pairs:
        v = " ".join(tokenize(variant))
        c = " ".join(tokenize(canonical))
        if v and c:
            _ALIASES[v] = c


# Ordinal suffixes: a human reads "AirPods Pro 2nd Gen" as the "2", so fold
# "2nd"/"3rd"/"11th" down to the bare digit before any matching. Applied to both
# query and title so a digit-form query pins an ordinal-form title and vice versa.
_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")


def canonicalize(text: str) -> str:
    """Normalise text and fold known short-hands to their canonical phrase."""
    norm = _ORDINAL_RE.sub(r"\1", " ".join(tokenize(text)))
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


# Edition designators — a lone letter after "series"/"one" (Xbox Series X vs S,
# Xbox One X vs S) or a generation after "mark"/"mk" (Canon R6 vs R6 Mark II).
# These name *different products at different prices* but share every other word,
# so neither coverage, fuzzy, nor semantics can tell them apart — they need an
# exact guardrail. Unlike model-line qualifiers they are not priced as clusters,
# so the check is symmetric: a watch and a listing must agree on the designator.
_SERIES_DESIGNATOR_RE = re.compile(r"\b(?:series|one)\s+([a-z])\b")
_MARK_DESIGNATOR_RE = re.compile(r"\b(?:mark|mk)\s*([0-9]+|[ivx]+)\b")
_ROMAN = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}


def series_designators(text: str) -> set[str]:
    """Lone model letters after "series"/"one" — the X/S in Xbox Series X/S."""
    return {m.group(1) for m in _SERIES_DESIGNATOR_RE.finditer(text)}


def mark_designators(text: str) -> set[str]:
    """Generation markers after "mark"/"mk", with roman numerals folded to digits
    so "Mark II" == "mk2" == "Mark 2"."""
    return {_ROMAN.get(m.group(1), m.group(1)) for m in _MARK_DESIGNATOR_RE.finditer(text)}


# Bare roman-numeral generations (Sony A7 III, Canon 5D IV) name a distinct,
# differently-priced product. Restricted to unambiguous II-IX: lone "i"/"v"/"x"
# are too noisy (and X is already the Xbox Series-letter), so they are excluded.
_GEN_ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix"}


def generation_designators(text: str) -> set[str]:
    """Standalone roman-numeral generation tokens, folded to digits ("iii" -> 3).

    A roman that directly follows "mark"/"mk" belongs to :func:`mark_designators`
    (so "Mark II" and "mk2" stay equivalent) and is not double-counted here.
    """
    tokens = text.split()
    return {
        _ROMAN[token]
        for index, token in enumerate(tokens)
        if token in _GEN_ROMAN and not (index and tokens[index - 1] in ("mark", "mk"))
    }


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


# Liquid volume — the size that defines a perfume or drink (100ml ≠ 60ml). "ml"
# and "cl" precede bare "l" in the alternation so "100ml" isn't read as "100" "l".
_VOLUME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|cl|fl\s*oz|oz|l)\b")


def volumes(text: str) -> set[str]:
    """Liquid-volume tokens in ``text`` ("100ml", "2l"), normalised and despaced."""
    out = set()
    for match in _VOLUME_RE.finditer(text.lower()):
        unit = match.group(2).replace(" ", "")
        out.add(f"{match.group(1)}{unit}")
    return out


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
    #    fuzzy/semantic layers consider them similar. A mixed letter+digit model
    #    code is matched tolerant of separators ("WH-1000XM5" ≡ "WH1000XM5") so a
    #    hyphen/space difference doesn't drop a genuine comparable; bare numbers
    #    ("13") stay an exact-token match so "13" never matches inside "2013".
    t_joined = t_norm.replace(" ", "")
    for disc in discriminators(query):
        if disc in t_tokens:
            continue
        if len(disc) >= 4 and any(ch.isalpha() for ch in disc) and disc in t_joined:
            continue
        # A long reference number often gains a sub-variant suffix in the title
        # (Rolex 116610 → 116610LN); accept it as a token prefix. Length ≥ 5 keeps
        # this safe — "13" is never a prefix of a longer number like "2013".
        if len(disc) >= 5 and any(token.startswith(disc) for token in t_tokens):
            continue
        return False

    # 3b) Edition designators (Xbox Series X vs S, Canon R6 vs R6 Mark II) name
    #     different products that share every other word, so query and title must
    #     agree on them exactly — in both directions.
    if series_designators(q_norm) != series_designators(t_norm):
        return False
    if mark_designators(q_norm) != mark_designators(t_norm):
        return False
    if generation_designators(q_norm) != generation_designators(t_norm):
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

    # 5b) A liquid volume the query pins (perfume 100ml, drink 2l) must be present.
    #     Only fires when the query names a volume, so non-volume watches are
    #     untouched while a 60ml never prices against a 100ml market.
    q_volumes = volumes(q_norm)
    if q_volumes and not q_volumes <= volumes(t_norm):
        return False

    # 6) Relevance: brand/key nouns present (coverage), OR a high fuzzy ratio
    #    (word order / typos), OR semantic similarity (reworded / cross-category).
    q_content = content_tokens(query)
    if not q_content:
        return True
    # Fold trivial plurals on both sides so "playstation 5 games" still covers a
    # "PlayStation 5 Game" title (a human reads them as the same word).
    q_stems = {_singular(token) for token in q_content}
    t_stems = {_singular(token) for token in t_tokens}
    if len(q_stems & t_stems) / len(q_stems) >= coverage:
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
