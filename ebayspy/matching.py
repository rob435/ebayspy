"""Decide whether a candidate listing is the *same product* as a market watch.

The Browse API keyword search is broad: a query like "dyson v11 absolute" also
returns batteries, wall mounts, spare parts, faulty units, empty boxes, replicas
and multi-unit lots. Pricing over that mix gives a meaningless median, so before
anything is priced each title is narrowed to listings that are a single,
complete, working unit of the product the query names. ``match_verdict`` runs a
fixed pipeline and reports the first rule that rejected (``is_comparable`` and
``filter_comparable`` are thin wrappers):

1. **Normalise** query and title alike. Lowercase and fold accents (für ->
   fur); keep decimals ("9.5", "1.5l", "f1.8"); unify aperture forms (f/1.8,
   F 1.8, 1:1.8); join hyphenated model codes (WH-1000XM5 -> wh1000xm5); fold
   ordinals (2nd, "second gen") to digits; glue numbers to their units ("50 mm"
   -> 50mm); expand aliases (ps5 -> playstation 5) in one pass. Glued words the
   query spells apart ("AirMax", "SeriesX", "Powerbank") are re-split against
   the query, and every token is also read as letter/digit segments so
   "IPHONE13", "A7III" and "Mini3" are seen as the words they contain.
2. **Clause structure.** The title is split at a compatibility marker ("for",
   "fits", "compatible", "like", and für/para/pour/per/voor/kompatibel...) and
   at an extras marker ("with", "w/", "+", "including", "inkl", "mit", "con",
   "in", or a count such as "2 Batteries"). Words after an extras marker are
   in-box extras, which never make a listing an accessory. A title that *starts*
   with a compatibility marker, or whose head before one does not carry the
   product, is an accessory for it.
3. **Listing-type guardrails**, each a stemmed lexicon read in context, with
   any word the query itself uses exempt: quantity (lots, "2 x", "x2", "pack
   of 2", "2 pairs"), damage (faulty, cracked, "not working", "iCloud locked",
   "as is", "80% full"), replica (replica, dupe, homage, "1:1", "display
   model"), packaging ("box only", "empty", "no headphones"), parts
   ("<component> only", "no controller"), accessories (an accessory noun,
   English or common foreign, outside the extras clause, outside the product's
   own name and not the configuration of a whole unit — "Aluminium Case with
   Sport Band", "Console ... Joy-Con Controllers"), compatibility lists ("S8 S9
   Headphones"), media (game, book, poster — not "games console") and larger
   host products ("Gaming PC ... RTX 4070", "250D with EF 50mm").
4. **Identity.** Every model code / number the query names must appear, read
   tolerant of how codes are written (DCD 796, DCD796N, AF300 for AF300UK) but
   bounded so X100V never matches X100VI and 13 never matches 2013 or A13.
   Generations attached to the product's name (Mark III == III == mk3) and
   series letters (Series X vs One X) are pinned both ways; pinned model-line
   qualifiers, and words a title *adds* (a tier suffix "Ti", "1st Edition",
   kids sizes, a foreign-language printing, a different short line code in the
   same slot "EF" -> "RF") name a different product. A sequel number the query
   leaves open ("Echo Dot 3rd Gen" for "echo dot") is an unpinned variant that
   pricing clusters with :func:`generation_of`.
5. **Variants the query pins** must agree: capacity, colour, liquid volume
   (100ml == 3.4 oz), a measurement (45mm vs 41mm), graded-card grade *with*
   its grader, shoe size with its system.
6. **Relevance.** Most of the query's words must appear (``coverage``), else a
   fuzzy ratio, else (in ``filter_comparable``) semantic similarity — computed
   lazily, only for the few titles that passed every guardrail.

Everything is rule-based, dependency-free (rapidfuzz optional) and cached: the
query is compiled once per ``filter_comparable`` call and per-title features are
memoised, since the same titles recur on every poll. Precision is favoured over
recall: a false accept poisons a median; a false reject only thins the sample.
"""

from __future__ import annotations

import functools
import itertools
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
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

DEFAULT_COVERAGE = 0.6

# Words that carry no identifying signal and must not count toward matching.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "with", "in", "on", "to", "by",
    "new", "brand", "genuine", "official", "original", "authentic", "boxed",
    "bnib", "bnwt", "uk", "eu", "us", "free", "fast", "postage", "delivery",
    "ship", "shipping", "sealed", "unused", "mint", "condition", "item", "items",
}

# ---------------------------------------------------------------------------
# Lexicons. Each is matched on stems (plural folded) and only when the query
# does not itself use the word; most are further restricted by position.
# ---------------------------------------------------------------------------

# Accessory / spare-part nouns. A title that carries one *outside* its in-box
# extras clause and outside the product's own name is the accessory, not the
# product ("Xbox Series X Controller", "Lens Hood for Canon EF 50mm").
ACCESSORY_TOKENS = {
    # mounting, protection, carrying
    "bracket", "mount", "holder", "stand", "dock", "docking", "cradle", "tripod",
    "case", "cover", "skin", "protector", "sleeve", "pouch", "bag", "decal",
    "sticker", "lanyard", "strap", "grip", "cage", "hood", "winder",
    # power and connectivity
    "charger", "adapter", "adaptor", "psu", "cable", "lead", "battery",
    # consumables and small parts
    "filter", "cartridge", "nozzle", "hose", "stylus", "liner", "tip", "pad",
    "cushion", "earpad", "lace", "insole", "propeller", "keyring", "keychain",
    # controls
    "controller", "remote",
    # spare parts
    "replacement", "spare", "part", "backplate", "housing", "chassis", "insert",
    "inlay", "digitizer", "handle", "chuck", "motherboard", "mainboard",
    "band", "charm", "accessory", "connector",
    # common non-English listings (accents are folded: hülle -> hulle)
    "armband", "hulle", "schutzhulle", "schutzfolie", "folie", "ladegerat",
    "ladekabel", "ladecase", "ladestation", "dockingstation", "kabel", "ohrstopsel",
    "ohrpolster", "zubehor", "ersatz", "ersatzteil", "lederband", "correa", "pulsera",
    "cargador", "funda", "carcasa", "coque", "cinturino", "custodia", "dragonne",
    "sangle", "chargeur", "etui", "housse", "ricambio", "sostituzione", "recambio",
    "repuesto", "pellicola",
}
_ACCESSORY_PHRASES = {
    ("logic", "board"), ("main", "board"), ("screen", "assembly"),
    ("display", "assembly"), ("expansion", "card"), ("memory", "card"),
    ("sd", "card"), ("sticker", "sheet"), ("remote", "control"), ("light", "kit"),
}
# Damage / not-working vocabulary (single words; phrases below).
DAMAGE_TOKENS = {
    "faulty", "fault", "broken", "damaged", "damage", "cracked", "crack",
    "smashed", "shattered", "snapped", "swollen", "burnt", "burning", "untested",
    "incomplete", "defective", "crashed", "fungus", "hole", "holes", "missing",
    "chipped", "spares", "repair", "repairs", "bricked", "needs", "crackling",
    "blacklisted", "barred", "impaired", "banned", "issue", "issues", "bootloop",
}
# Multi-unit vocabulary. A "bundle" is one unit with extras, so it is a lot
# only when counted ("bundle of 5", read by lot_quantity).
LOT_TOKENS = {"lot", "lots", "joblot", "joblots", "wholesale", "bulk", "multipack"}
EXCLUSION_TOKENS = ACCESSORY_TOKENS | DAMAGE_TOKENS | LOT_TOKENS

REPLICA_TOKENS = {
    "replica", "fake", "clone", "dummy", "homage", "dupe", "proxy", "reprint",
    "inspired", "imitation", "counterfeit", "knockoff", "lookalike", "unbranded",
    "1:1",
}
_REPLICA_PHRASES = [
    ("display", "model"), ("non", "genuine"), ("not", "genuine"), ("knock", "off"),
    ("look", "alike"), ("fan", "art"),
]
_DAMAGE_PHRASES = [
    ("for", "parts"), ("parts", "only"), ("sold", "as", "seen"),
    ("read", "description"), ("dead", "pixel"), ("lines", "on", "screen"),
    ("screen", "burn"), ("burn", "in"), ("icloud", "locked"),
    ("activation", "locked"), ("mdm", "locked"), ("google", "locked"),
    ("frp", "locked"), ("account", "locked"), ("bad", "imei"), ("ic", "locked"),
    ("activation", "lock"), ("as", "is"), ("boot", "loop"), ("bad", "esn"),
]
# "iCloud" alone flags a locked phone unless it says the lock is off.
_ICLOUD_OK = {"clean", "free", "unlocked", "off", "removed", "ready", "ok", "clear"}
# "<negator> <function verb>" — "not working", "stops working", "won't charge".
_NEGATORS = {
    "not", "wont", "cant", "cannot", "doesnt", "dont", "stops", "stopped", "fails",
    "failed",
}
_APOSTROPHE_NEGATORS = {"won", "doesn", "can", "don", "isn", "wasn", "didn"}
_FUNCTION_VERBS = {
    "work", "working", "works", "charge", "charging", "turn", "turning", "power",
    "powering", "heat", "heating", "run", "running", "read", "reading", "boot",
    "booting", "spin", "spinning", "connect", "connecting", "pair", "pairing",
    "respond", "responding", "function", "functioning", "cool", "cooling", "load",
    "loading", "hold", "holding", "start", "starting", "play", "playing", "focus",
    "focusing", "tested", "detected", "recognised", "recognized",
}
# "no <function>" — "No Power", "no display".
_NO_FUNCTION = {
    "power", "sound", "display", "signal", "picture", "image", "video", "audio",
    "wifi", "bluetooth", "charge", "charging", "boot", "suction", "heat", "face",
    "touch",
}
# "<n>% full" — a partly used consumable or partly complete set.
_PARTIAL_WORDS = {"full", "left", "remaining", "remains", "complete"}
# Damage words next to these describe the packaging, not the item.
_PACKAGING_DAMAGE = {"box", "packaging", "outer", "tin"}

_PACKAGING_WORDS = {
    "box", "packaging", "papers", "paperwork", "manual", "instructions", "receipt",
    "tag", "bottle",
}
# Nouns naming a whole unit: "<whole> only" means the item without extras, and
# an accessory noun followed by one is a modifier ("Smart Controller Drone").
_WHOLE_ITEMS = {
    "console", "camera", "body", "drone", "aircraft", "phone", "smartphone",
    "handset", "tv", "television", "laptop", "tablet", "watch", "smartwatch",
    "tracker", "unit", "device", "machine", "vacuum", "drill", "fryer", "mixer",
    "speaker", "monitor", "printer", "router", "lens",
}
# "<word> only" that still describes a complete unit: logistics ("collection
# only") and connectivity variants ("WiFi only", "GPS only").
_OK_BEFORE_ONLY = (_WHOLE_ITEMS - {"tablet"}) | {
    "collection", "collect", "pickup", "pick", "postage", "delivery", "uk",
    "local", "cash", "once", "twice", "sale", "gps", "wifi", "fi", "cellular", "lte",
    "unlocked", "bluetooth", "wireless",
}
# Watch-case vocabulary: "Aluminium Case with Sport Band" is the watch itself.
_CASE_MATERIALS = {
    "aluminium", "aluminum", "alluminum", "steel", "stainless", "titanium", "ceramic",
}
_BAND_WORDS = {"band", "armband", "strap", "loop", "buckle", "bracelet"}
_FREQUENCY_BANDS = {"dual", "tri", "single", "quad", "multi", "wide", "broad"}
_LISTENING = {"headphones", "earbuds", "earphones"}
# Components without which a unit is incomplete ("Console Only No Controller").
_INTEGRAL_COMPONENTS = {
    "controller", "joy", "joycon", "tool", "attachment", "dock", "gamepad",
    "headband", "earpad", "pad", "cushion",
}
MEDIA_TOKENS = {
    "game", "book", "poster", "dvd", "bluray", "cd", "vinyl", "magazine",
    "cookbook", "novel", "comic", "artbook",
}
# A larger product that merely contains the watched one ("Gaming PC ... RTX").
_HOST_NOUNS = {"pc", "laptop", "desktop", "computer", "notebook", "workstation"}
_AUDIENCE = {
    "kid", "kids", "junior", "jnr", "youth", "infant", "toddler", "baby", "child",
    "children", "childrens", "boys", "girls",
}
_LANGUAGES = {"japanese", "chinese", "korean"}
# A tier word directly after a model number ("RTX 4070 Ti", "4070 SUPER").
_TIER_SUFFIXES = {"ti", "super", "xt", "xtx"}

_EXTRAS_MARKERS = {
    "with", "w", "+", "inc", "incl", "including", "includes", "in", "inkl", "mit", "con",
    "avec",
}
_COMPAT_MARKERS = {
    "for", "fits", "compatible", "compat", "suits", "suitable", "like", "replaces",
    "fur", "fuer", "passend", "para", "pour", "per", "voor", "kompatibel", "ersetzt",
}
# Markers that make a title starting with them an accessory ("For iPhone 7 ...").
_INITIAL_COMPAT = _COMPAT_MARKERS - {"like", "per", "replaces", "suits"}
_NOT_COMPAT_AFTER = {"parts", "spares", "repair", "repairs", "sale"}
# Words after an accessory noun that make it an included/absent feature.
_EXCUSE_NEXT = {
    "free", "included", "inc", "incl", "supplied", "fitted", "health", "life", "version",
}
_QTY_NOUNS = {"pack", "packs", "pk", "pcs", "units", "pairs", "multipack"}

# Model-line qualifiers. They distinguish *different products* at different
# prices ("iphone 13" vs "13 pro max"): a qualifier the query names is pinned
# exactly; an unnamed one is kept so the pricing stage can cluster it.
QUALIFIERS = {
    "pro", "max", "mini", "plus", "ultra", "se", "air", "lite", "digital",
    "slim", "premium", "deluxe", "oled",
    # Fragrance concentration — a different product at a different price.
    "edp", "edt", "edc", "parfum", "elixir", "extrait",
}
# Reading order for a multi-qualifier label, e.g. {"pro", "max"} -> "pro max".
QUALIFIER_ORDER = [
    "pro", "max", "plus", "ultra", "mini", "se", "air", "lite", "oled", "digital",
    "slim", "premium", "deluxe", "edp", "edt", "edc", "parfum", "elixir", "extrait",
]
# "air" names a model line (MacBook Air, Air Max) but not in an appliance name.
_AIR_COMPOUNDS = {
    "fryer", "fryers", "purifier", "conditioner", "pump", "compressor", "bed",
    "freshener", "mattress", "cooler", "humidifier", "dehumidifier", "rifle",
}

# Variant attributes — same product, different flavour. Pinned when the query
# names them; otherwise the service prices each variant separately. Words that
# double as a condition or filler ("mint", "clear", "space") are omitted.
COLOURS = {
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
    "pink", "grey", "gray", "silver", "gold", "bronze", "brown", "beige",
    "navy", "teal", "maroon", "olive", "indigo", "violet", "rose", "graphite",
    "charcoal", "midnight", "starlight", "titanium", "champagne", "ivory",
    "cream", "burgundy", "lavender", "turquoise", "gunmetal",
}
_COLOUR_ALIASES = {"gray": "grey"}

_ROMAN = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}
# Bare roman generations; lone i/v/x are too noisy (X is also a series letter).
_GEN_ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix"}
_SPELLED_ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12",
}
_ORDINAL_NOUNS = {"gen", "generation", "edition", "ed", "series"}
_SYNONYMS = {"generation": "gen"}
_GRADERS = ("psa", "bgs", "cgc", "sgc")
_SIZE_SYSTEMS = ("uk", "us", "eu")

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_PLUS_RE = re.compile(r"(?<=[a-z0-9])\+")  # "S21+" is the Plus model line
_WITH_SLASH_RE = re.compile(r"\bw/")
_APERTURE_RES = (
    (re.compile(r"\bf\s*/\s*(\d+(?:\.\d+)?)"), r"f\1"),     # f/1.8
    (re.compile(r"\bf\s+(\d+\.\d+)\b"), r"f\1"),            # F 1.8
    (re.compile(r"(?<![\d.:])1\s*:\s*(\d+\.\d+)\b"), r"f\1"),  # 1:1.8
)
# Graded cards ("PSA10") and shoe sizes ("UK9") are written glued or spaced.
_GRADE_RE = re.compile(r"\b(psa|bgs|cgc|sgc)\s*(\d+(?:\.\d+)?)\b")
_SIZE_GLUE_RE = re.compile(r"\b(uk|us|eu)(\d+(?:\.\d+)?)\b")
_HYPHEN_PAIR_RE = re.compile(r"([a-z0-9]+)-([a-z0-9]+)")
_WORD_RE = re.compile(r"[a-z0-9]*\d(?:\.\d+)+[a-z0-9]*|[a-z0-9]+")
# Internal tokens also keep fractions (card numbers 4/102), ratios (1:1),
# percentages (80%) and a standalone "+" (an extras marker).
_LEX_RE = re.compile(
    r"\d+/\d+|\d+:\d+|\d+(?:\.\d+)?%|\+|[a-z0-9]*\d(?:\.\d+)+[a-z0-9]*|[a-z0-9]+"
)
_ORDINAL_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")
_SEG_RE = re.compile(r"[a-z]+|\d+(?:\.\d+)?")
_UNIT_JOIN = {
    "mm", "cm", "ml", "cl", "l", "gb", "tb", "mb", "inch", "mah", "mp", "kg", "v",
    "w", "oz", "hz",
}


def _join_model_hyphens(text: str) -> str:
    """Fold the hyphen out of a model code so its written forms unify.

    "WH-1000XM5", "SM-G991B" and "F-150" read as one token; the digit is the
    tell, so ordinary hyphenated words ("t-shirt", "wi-fi") stay apart.
    """

    def repl(match: re.Match) -> str:
        left, right = match.group(1), match.group(2)
        if any(c.isdigit() for c in left) or any(c.isdigit() for c in right):
            return left + right
        return match.group(0)

    return _HYPHEN_PAIR_RE.sub(repl, text)


def _fold_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _prepare(text: str) -> str:
    lowered = _PLUS_RE.sub(" plus", _fold_accents((text or "").lower()))
    for pattern, repl in _APERTURE_RES:
        lowered = pattern.sub(repl, lowered)
    lowered = _GRADE_RE.sub(r"\1 \2", lowered)
    return _join_model_hyphens(lowered)


def tokenize(text: str) -> list[str]:
    """Lowercase word/number tokens; decimals ("9.5", "1.5l") stay whole."""
    return _WORD_RE.findall(_prepare(text))


def normalize(text: str) -> str:
    return " ".join(tokenize(text))


def _fold_ordinals(tokens: list[str]) -> list[str]:
    """"2nd" -> "2", and spelled "second gen" -> "2 gen"."""
    out = []
    for index, token in enumerate(tokens):
        match = _ORDINAL_RE.match(token)
        if match:
            token = match.group(1)
        elif token in _SPELLED_ORDINALS:
            nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
            if nxt in _ORDINAL_NOUNS:
                token = _SPELLED_ORDINALS[token]
        out.append(token)
    return out


# Common short-hands mapped to a canonical phrase so a "ps5" watch matches
# "PlayStation 5" titles and vice versa. Extensible via register_aliases.
DEFAULT_ALIASES = {
    "ps5": "playstation 5",
    "ps4": "playstation 4",
    "ps3": "playstation 3",
    "series x": "xbox series x",
    "series s": "xbox series s",
    "air pods": "airpods",
    "mac book": "macbook",
    "i phone": "iphone",
    # Fragrance concentration long-forms fold to their codes.
    "eau de parfum": "edp",
    "eau de toilette": "edt",
    "eau de cologne": "edc",
}
_ALIASES = dict(DEFAULT_ALIASES)
_ALIAS_RE: re.Pattern | None = None


def _compile_aliases() -> None:
    global _ALIAS_RE
    keys = sorted(_ALIASES, key=len, reverse=True)
    body = "|".join(re.escape(key) for key in keys)
    _ALIAS_RE = re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])") if keys else None


def _apply_aliases(norm: str) -> str:
    if _ALIAS_RE is None:
        return norm

    def repl(match: re.Match) -> str:
        variant = match.group(0)
        canonical = _ALIASES[variant]
        # "series x" -> "xbox series x" must not re-expand "xbox series x".
        if canonical.endswith(variant):
            prefix = canonical[: -len(variant)]
            if norm[: match.start()].endswith(prefix):
                return variant
        return canonical

    return _ALIAS_RE.sub(repl, norm)


_compile_aliases()


def register_aliases(pairs: Iterable[tuple[str, str]]) -> None:
    """Add short-hand -> canonical phrase aliases (normalised like any text)."""
    for variant, canonical in pairs:
        v = " ".join(_fold_ordinals(tokenize(variant)))
        c = " ".join(_fold_ordinals(tokenize(canonical)))
        if v and c:
            _ALIASES[v] = c
    _compile_aliases()
    _doc_from_tokens.cache_clear()
    _title_doc.cache_clear()
    _compile.cache_clear()


def canonicalize(text: str) -> str:
    """Normalise text, fold ordinals and expand known short-hands."""
    return _apply_aliases(" ".join(_fold_ordinals(tokenize(text))))


def _lex(text: str) -> list[str]:
    """Internal tokens: canonical, unit-joined, with structural markers kept."""
    prepared = _WITH_SLASH_RE.sub(" with ", _prepare(text))
    prepared = _SIZE_GLUE_RE.sub(r"\1 \2", prepared)
    tokens = _fold_ordinals(_LEX_RE.findall(prepared))
    joined: list[str] = []
    for token in tokens:
        if joined and token in _UNIT_JOIN and _NUMBER_RE.match(joined[-1]):
            joined[-1] += token
        else:
            joined.append(token)
    return _apply_aliases(" ".join(joined)).split()


@functools.lru_cache(maxsize=16384)
def _stems(token: str) -> frozenset[str]:
    """The token plus its singular forms and synonyms ("cases" -> "case")."""
    out = {token}
    if token in _SYNONYMS:
        out.add(_SYNONYMS[token])
    if token.isalpha() and len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        out.add(token[:-1])
        if token.endswith("es") and len(token) > 4:
            out.add(token[:-2])
        if token.endswith("ies") and len(token) > 4:
            out.add(token[:-3] + "y")
    return frozenset(out)


def _in(token: str, lexicon: set[str] | frozenset[str]) -> bool:
    return not _stems(token).isdisjoint(lexicon)


def _segments(token: str) -> list[str]:
    """Letter/digit runs of a token ("iphone13pro" -> iphone, 13, pro)."""
    if not token[:1].isalnum() or not token.replace(".", "").isalnum():
        return [token]
    return _SEG_RE.findall(token) or [token]


def _is_count(token: str, high: int = 20) -> int | None:
    if token.isdigit() and 2 <= int(token) <= high:
        return int(token)
    return None


def _is_plural(token: str) -> bool:
    return (
        token.isalpha() and len(token) > 3 and token.endswith("s")
        and not token.endswith(("ss", "us", "is")) and token not in QUALIFIERS
    )


def content_tokens(text: str) -> set[str]:
    """Identifying tokens: drop stopwords and single characters."""
    return {token for token in tokenize(text) if token not in STOPWORDS and len(token) >= 2}


# Measurement/storage units: incidental specs, not model identifiers.
_DISCRIM_SKIP_RE = re.compile(
    r"^\d+(?:\.\d+)?(v|hz|khz|mhz|ghz|w|kw|gb|tb|mb|mm|cm|m|inch|in|ft|hp|ml|cl|l|kg|g|"
    r"mg|mah|ah|wh|k|mp|oz|p|core)$"
)
_MARK_TOKEN_RE = re.compile(r"^(?:mk|mark)(?:\d+|[ivx]+)$")


def discriminators(text: str) -> set[str]:
    """Digit-bearing tokens that pin product identity (13, hu02, g991b, qe55).

    These separate near-identical products (iPhone 13 vs 14, PS4 vs PS5), so
    they are required in a comparable title; fuzzy and semantic relevance may
    relax *words*, never these. Measurement and storage units are excluded.
    """
    return {
        token
        for token in tokenize(text)
        if any(ch.isdigit() for ch in token)
        and token not in STOPWORDS
        and not _DISCRIM_SKIP_RE.match(token)
    }


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------

_CAPACITY_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(gb|tb)\b"
    r"(?!\s*(?:of\s+)?(?:ram|memory|ddr\d*|lpddr\w*|unified))"
)
_VOLUME_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(ml|cl|fl\s*oz|oz|litres?|liters?|ltr|l)\b"
)
_VOLUME_ML = {"ml": 1.0, "cl": 10.0, "l": 1000.0, "floz": 29.5735, "oz": 29.5735}
_GRADE_PAIR_RE = re.compile(
    rf"\b({'|'.join(_GRADERS)})\s+(?:(?:gem|mint|nm|mt|near|pristine|grade)\s+)*"
    r"(\d+(?:\.\d+)?)(?![\d.])"
)
_SIZE_PAIR_RES = (
    re.compile(r"\b(uk|us|eu)\s+(?:size\s+)?(\d+(?:\.\d+)?)(?![\d.])"),
    re.compile(r"\bsize\s+(?:(uk|us|eu)\s+)?(\d+(?:\.\d+)?)(?![\d.])(?:\s+(uk|us|eu)\b)?"),
)


def canonical_colours(tokens: set[str]) -> set[str]:
    """Colour tokens from ``tokens``, folding spelling synonyms (e.g. gray→grey)."""
    return {_COLOUR_ALIASES.get(token, token) for token in tokens if token in COLOURS}


def _number(value: str) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def normalize_capacity(value: str) -> str | None:
    """Canonicalise a capacity string ("256 GB", "256GB") to "256gb"."""
    match = _CAPACITY_RE.search((value or "").lower())
    return f"{_number(match.group(1))}{match.group(2)}" if match else None


_ANY_CAPACITY_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(gb|tb)\b")
_MEASURE_RE = re.compile(r"^(\d+(?:\.\d+)?)(mm|cm|inch|mah|w|kg)$")


def _capacities(text: str, storage_only: bool = True) -> frozenset[str]:
    """Capacities in ``text``; RAM ("16GB RAM") only when not ``storage_only``."""
    pattern = _CAPACITY_RE if storage_only else _ANY_CAPACITY_RE
    return frozenset(f"{_number(m.group(1))}{m.group(2)}" for m in pattern.finditer(text))


def _measures(tokens: Sequence[str]) -> dict[str, set[str]]:
    """Dimension-like measurements by unit: {"mm": {"45"}} for "45mm"."""
    out: dict[str, set[str]] = {}
    for token in tokens:
        match = _MEASURE_RE.match(token)
        if match:
            out.setdefault(match.group(2), set()).add(_number(match.group(1)))
    return out


def volumes(text: str) -> set[str]:
    """Liquid-volume tokens in ``text`` ("100ml", "1.5l"), normalised and despaced."""
    out = set()
    for match in _VOLUME_RE.finditer((text or "").lower()):
        unit = match.group(2).replace(" ", "")
        unit = "l" if unit.startswith(("lit", "ltr")) else unit
        out.add(f"{match.group(1)}{unit}")
    return out


def _volumes_ml(text: str) -> tuple[float, ...]:
    out = []
    for match in _VOLUME_RE.finditer(text):
        unit = match.group(2).replace(" ", "")
        unit = "l" if unit.startswith(("lit", "ltr")) else unit
        out.append(float(match.group(1)) * _VOLUME_ML[unit])
    return tuple(out)


def _same_volume(a: float, b: float) -> bool:
    return abs(a - b) <= 0.03 * max(a, b)  # 3.4 fl oz is sold as 100ml


def _qualifiers(segs: Sequence[str]) -> frozenset[str]:
    out = set()
    for index, seg in enumerate(segs):
        if seg in QUALIFIERS:
            nxt = segs[index + 1] if index + 1 < len(segs) else ""
            if seg == "air" and nxt in _AIR_COMPOUNDS:
                continue
            out.add(seg)
    return frozenset(out)


def _generations(segs: Sequence[str], anchors: frozenset | None = None) -> frozenset[str]:
    """Generation markers: "Mark II" == "mk2" == bare "II" (folded to "2").

    With ``anchors`` (the query's own segments) only markers attached to the
    product's name count — "A7 III", "R6 Mark II" — not a roman numeral in
    some other name ("PS4 Final Fantasy VII Pack").
    """
    out = set()
    for index, seg in enumerate(segs):
        prev = segs[index - 1] if index else ""
        nxt = segs[index + 1] if index + 1 < len(segs) else ""
        attached = anchors is None or prev in anchors
        if seg in ("mark", "mk") and (nxt.isdigit() or nxt in _ROMAN):
            if attached:
                out.add(_ROMAN.get(nxt, nxt))
        elif seg in _GEN_ROMAN and prev not in ("mark", "mk") and attached:
            out.add(_ROMAN[seg])
    return frozenset(out)


def _series(segs: Sequence[str]) -> frozenset[tuple[str, str]]:
    """Edition letters: ("series", "x") for Xbox Series X, ("one", "s") for One S."""
    out = set()
    for index, seg in enumerate(segs[:-1]):
        nxt = segs[index + 1]
        if seg in ("series", "one") and len(nxt) == 1 and nxt.isalpha():
            out.add((seg, nxt))
    return frozenset(out)


def _sizes(seg_text: str) -> frozenset[tuple[str, str]]:
    out = set()
    for match in _SIZE_PAIR_RES[0].finditer(seg_text):
        out.add((match.group(1), _number(match.group(2))))
    for match in _SIZE_PAIR_RES[1].finditer(seg_text):
        system = match.group(1) or match.group(3) or "size"
        out.add((system, _number(match.group(2))))
    return frozenset(out)


def _lot_quantity(tokens: Sequence[str]) -> int | None:
    found = []
    n = len(tokens)
    for i, token in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < n else ""
        nxt2 = tokens[i + 2] if i + 2 < n else ""
        if token in ("lot", "bundle", "pack", "set", "box", "case", "joblot") and nxt == "of":
            found.append(_is_count(nxt2, 200))
        if token in ("lot", "joblot"):
            glued = re.fullmatch(r"(\d+)x|x(\d+)", nxt)
            found.append(_is_count(nxt, 200) or (glued and int(glued.group(1) or glued.group(2))))
        if _is_count(token, 200) and nxt in _QTY_NOUNS:
            found.append(int(token))
        glued = re.fullmatch(r"(\d+)(pack|pk|pcs|pairs|units)", token)
        if glued:
            found.append(_is_count(glued.group(1), 200))
        if token in ("twin", "dual", "double", "triple") and nxt in ("pack", "pk"):
            found.append(3 if token == "triple" else 2)
    if n:
        # "2 x iPhone", "2x DeWalt" at the start; "... x2", "... x 2" at the end.
        lead = 0
        while lead < n - 1 and tokens[lead] in ("job", "lot", "joblot", "of"):
            lead += 1
        head = re.fullmatch(r"(\d+)x", tokens[lead])
        if head:
            found.append(_is_count(head.group(1), 24))
        elif lead + 1 < n and tokens[lead + 1] == "x":
            found.append(_is_count(tokens[lead], 24))
        tail = re.fullmatch(r"x(\d+)", tokens[-1])
        if tail:
            found.append(_is_count(tail.group(1), 24))
        elif n >= 2 and tokens[-2] == "x":
            found.append(_is_count(tokens[-1], 24))
    counts = [count for count in found if count and 2 <= count <= 200]
    return max(counts) if counts else None


# ---------------------------------------------------------------------------
# Per-title analysis (memoised)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Doc:
    tokens: tuple[str, ...]
    text: str                       # tokens joined by single spaces
    first_pos: dict                 # stem -> first token index carrying it
    extras_start: int               # first token of the in-box extras clause
    extras_explicit: bool           # opened by a marker word, not a count
    compat_at: int                  # index of a "for"/"fits"/"like" marker, or -1
    head_segs: tuple[str, ...]      # segments before the extras clause
    qualifiers: frozenset
    generations: frozenset
    series: frozenset
    capacities: frozenset
    colours: frozenset
    volumes_ml: tuple
    grades: frozenset
    sizes: frozenset
    lot_qty: int | None
    hits: tuple                     # (family, detail, trigger tokens), ordered
    accessories: tuple              # (index, word, trigger tokens)
    wholes: tuple                   # (index, token) of whole-unit nouns in the head
    hosts: tuple                    # (index, word)


_FAMILY_ORDER = ("quantity", "damage", "replica", "packaging", "part", "media")


def _has_content(tokens: Sequence[str], minimum: int = 1) -> bool:
    count = sum(1 for t in tokens if t not in STOPWORDS and t[:1].isalnum() and len(t) >= 2)
    return count >= minimum


def _clauses(tokens: Sequence[str]) -> tuple[int, bool, int]:
    n = len(tokens)
    extras, explicit = n, False
    for i, token in enumerate(tokens):
        if token in _EXTRAS_MARKERS and 0 < i < n - 1 and _has_content(tokens[:i], 2):
            extras, explicit = i, True
            break
        if (
            i >= 2 and _is_count(token) and i + 1 < n
            and _is_plural(tokens[i + 1]) and tokens[i + 1] not in _QTY_NOUNS
        ):
            extras = i
            break
    compat = -1
    for i, token in enumerate(tokens):
        if token not in _COMPAT_MARKERS or i >= n - 1 or tokens[i + 1] in _NOT_COMPAT_AFTER:
            continue
        if (i == 0 and token in _INITIAL_COMPAT) or (i and _has_content(tokens[:i])):
            compat = i
            break
    return extras, explicit, compat


def _phrase_at(tokens: Sequence[str], i: int, phrase: Sequence[str]) -> bool:
    window = tokens[i:i + len(phrase)]
    return len(window) == len(phrase) and all(w in _stems(t) for t, w in zip(window, phrase))


def _content_hits(tokens: Sequence[str], extras: int, lot_qty: int | None) -> list:
    """Listing-type signals that do not depend on the query."""
    hits: list[tuple[str, str, tuple[str, ...]]] = []
    n = len(tokens)
    if lot_qty:
        hits.append(("quantity", str(lot_qty), ()))
    for i, token in enumerate(tokens):
        prev = tokens[i - 1] if i else ""
        nxt = tokens[i + 1] if i + 1 < n else ""
        if token in LOT_TOKENS:
            hits.append(("quantity", "lot", (token,)))
        # damage
        if _in(token, DAMAGE_TOKENS) and not (
            prev in _PACKAGING_DAMAGE or nxt in _PACKAGING_DAMAGE
        ):
            hits.append(("damage", token, (token,)))
        if token == "icloud" and nxt not in _ICLOUD_OK and nxt != "locked":
            hits.append(("damage", token, (token,)))
        for phrase in _DAMAGE_PHRASES:
            if _phrase_at(tokens, i, phrase):
                hits.append(("damage", " ".join(phrase), phrase))
        negated = token in _NEGATORS or (token in _APOSTROPHE_NEGATORS and nxt == "t")
        verb_at = i + 2 if token in _APOSTROPHE_NEGATORS else i + 1
        if negated and verb_at < n and tokens[verb_at] in _FUNCTION_VERBS:
            hits.append(("damage", f"{token} {tokens[verb_at]}", (token, tokens[verb_at])))
        if token in ("no", "without") and nxt in _NO_FUNCTION:
            hits.append(("damage", f"no {nxt}", (token, nxt)))
        if token.endswith("%") and nxt in _PARTIAL_WORDS and float(token[:-1]) < 100:
            hits.append(("damage", f"{token} {nxt}", (nxt,)))
        # replica
        if token in REPLICA_TOKENS or _in(token, REPLICA_TOKENS):
            hits.append(("replica", token, (token,)))
        for phrase in _REPLICA_PHRASES:
            if _phrase_at(tokens, i, phrase):
                hits.append(("replica", " ".join(phrase), phrase))
        # packaging and parts
        if token == "empty":
            hits.append(("packaging", "empty", (token,)))
        if token == "only" and i:
            j = i - 1
            while j > 0 and (_NUMBER_RE.match(tokens[j]) or tokens[j] in ("and", "&")):
                j -= 1
            word = tokens[j]
            if _in(word, _PACKAGING_WORDS):
                hits.append(("packaging", f"{word} only", (word,)))
            elif not (
                _in(word, _OK_BEFORE_ONLY) or word in STOPWORDS or word in COLOURS
                or not word.isalpha()
            ):
                hits.append(("part", f"{word} only", (word,)))
        if token in ("no", "without") and _in(nxt, _INTEGRAL_COMPONENTS):
            hits.append(("part", f"no {nxt}", (token, nxt)))
        if token in ("no", "without") and _in(nxt, _WHOLE_ITEMS | _LISTENING):
            hits.append(("packaging", f"no {nxt}", (token, nxt)))  # "Box - No Headphones"
        # media, outside the extras clause and not naming a device type
        # ("Video Game Console", "Games console")
        if (
            i < extras and _in(token, MEDIA_TOKENS)
            and not any(_in(t, _WHOLE_ITEMS) for t in tokens[i + 1:i + 3])
        ):
            hits.append(("media", token, (token,)))
    hits.sort(key=lambda hit: _FAMILY_ORDER.index(hit[0]))
    return hits


def _is_watch_case(tokens: Sequence[str], i: int, look_ahead: bool = True) -> bool:
    """"Aluminium Case", "42mm Case", "Case with Sport Band": a watch's case."""
    before = tokens[max(0, i - 4):i]
    after = tokens[i + 1:i + 5] if look_ahead else ()
    return (
        any(t in _CASE_MATERIALS or re.fullmatch(r"\d+(?:\.\d+)?mm", t) for t in before)
        or any(_in(t, _BAND_WORDS) for t in after)
    )


def _accessory_candidates(tokens: Sequence[str], extras: int) -> list:
    """Accessory nouns that are not excused by their own context."""
    out = []
    watch = any(t == "case" and _is_watch_case(tokens, i) for i, t in enumerate(tokens))
    for i in range(min(extras, len(tokens))):
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if (token, nxt) in _ACCESSORY_PHRASES:
            word, triggers = f"{token} {nxt}", (token, nxt)
        elif token in ACCESSORY_TOKENS or (
            token not in DAMAGE_TOKENS and _in(token, ACCESSORY_TOKENS)
        ):
            word, triggers = token, (token,)
        else:
            continue
        if nxt in _EXCUSE_NEXT or nxt.endswith("%"):
            continue  # "Filter-Free", "Remote Included", "Battery 79%"
        if watch and (token == "case" or _in(token, _BAND_WORDS)):
            continue  # the watch's own case and band
        if _in(token, _BAND_WORDS) and _is_watch_case(tokens, i, look_ahead=False):
            continue  # "42mm Aluminum White Band": a configured watch's band
        if token == "band" and i and tokens[i - 1] in _FREQUENCY_BANDS:
            continue  # "Dual Band" router: a radio band, not a strap
        out.append((i, word, triggers))
    return out


@functools.lru_cache(maxsize=4096)
def _doc_from_tokens(tokens: tuple[str, ...]) -> _Doc:
    seg_lists = [_segments(token) for token in tokens]
    first_pos: dict[str, int] = {}
    for i, (token, segs) in enumerate(zip(tokens, seg_lists)):
        for piece in (token, *segs):
            for stem in _stems(piece):
                first_pos.setdefault(stem, i)
    extras, explicit, compat = _clauses(tokens)
    head_segs = tuple(seg for segs in seg_lists[:extras] for seg in segs)
    all_segs = [seg for segs in seg_lists for seg in segs]
    text = " ".join(tokens)
    seg_text = " ".join(all_segs)
    lot_qty = _lot_quantity(tokens)
    return _Doc(
        tokens=tokens,
        text=text,
        first_pos=first_pos,
        extras_start=extras,
        extras_explicit=explicit,
        compat_at=compat,
        head_segs=head_segs,
        qualifiers=_qualifiers(head_segs),
        generations=_generations(head_segs),
        series=_series(head_segs),
        capacities=_capacities(text),
        colours=frozenset(canonical_colours(set(tokens) | set(all_segs))),
        volumes_ml=_volumes_ml(text),
        grades=frozenset(
            (m.group(1), _number(m.group(2))) for m in _GRADE_PAIR_RE.finditer(seg_text)
        ),
        sizes=_sizes(seg_text),
        lot_qty=lot_qty,
        hits=tuple(_content_hits(tokens, extras, lot_qty)),
        accessories=tuple(_accessory_candidates(tokens, extras)),
        hosts=tuple((i, t) for i, t in enumerate(tokens[:extras]) if t in _HOST_NOUNS),
        wholes=tuple((i, t) for i, t in enumerate(tokens[:extras]) if _in(t, _WHOLE_ITEMS)),
    )


@functools.lru_cache(maxsize=4096)
def _title_doc(title: str) -> _Doc:
    return _doc_from_tokens(tuple(_lex(title)))


def lot_quantity(title: str) -> int | None:
    """Number of units in a lot/multipack title, or None if it isn't one.

    Reads "lot of 5", "job lot 4", "2 x ..." / "2x ..." at the start, "... x2" /
    "... x 2" at the end, "pack of 2", "2 pack", "twin pack" and "2 pairs".
    Model names and specs ("Ventus 2X", "iPhone X 128 GB", "Series X 2
    Controllers", "Left Right Pair") are not quantities.
    """
    return _title_doc(title or "").lot_qty


def attributes(text: str) -> dict[str, object]:
    """Extract variant attributes from a title or query.

    Returns ``capacity`` / ``colour`` as a single canonical value (or None when
    absent/ambiguous) and ``qualifier`` as a model-line label that is always
    present ("base" when none) — these drive clustering. Also returns the raw
    sets. RAM ("16GB RAM") is not a storage capacity, and qualifiers are read
    from the title before any in-box extras clause.
    """
    doc = _title_doc(text or "")
    capacities = set(doc.capacities)
    colours = set(doc.colours)
    qualifiers = set(doc.qualifiers)
    return {
        "capacity": next(iter(capacities)) if len(capacities) == 1 else None,
        "colour": ",".join(sorted(colours)) if colours else None,
        "qualifier": _qualifier_label(qualifiers),
        "capacities": capacities,
        "colours": colours,
        "qualifiers": qualifiers,
    }


def _qualifier_label(qualifiers: set[str]) -> str:
    """Canonical label for a model line: "base", "pro", "pro max", …"""
    if not qualifiers:
        return "base"
    order = {name: i for i, name in enumerate(QUALIFIER_ORDER)}
    return " ".join(sorted(qualifiers, key=lambda q: (order.get(q, len(order)), q)))


def specified_dimensions(query: str) -> set[str]:
    """Variant dimensions the query already pins (so they need not be clustered)."""
    attrs = attributes(query)
    dims = {dim for dim in ("capacity", "colour") if attrs[dim]}
    if attrs["qualifiers"]:
        dims.add("qualifier")
    return dims


# ---------------------------------------------------------------------------
# Model codes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Code:
    token: str
    patterns: tuple  # (compiled regex, kind, joined code string)


def _code(token: str) -> _Code:
    """Compile the written forms of a model code / number.

    "main" allows a space at each letter/digit boundary (DCD 796, V 11);
    "base" drops a trailing letter suffix from a long code (AF300UK -> AF300);
    "lead" drops a leading letter prefix from a long code (WH1000XM5 ->
    1000XM5, at a token start only); "bare" reads f1.8 as a bare 1.8.
    """
    if not re.fullmatch(r"[a-z0-9.]+", token):
        exact = re.compile(rf"(?<!\S){re.escape(token)}(?!\S)")
        return _Code(token, ((exact, "exact", token),))
    parts = _SEG_RE.findall(token)
    patterns = []

    def add(ps: list[str], kind: str) -> None:
        strict = kind in ("lead", "bare") or ps[0][0].isalpha()
        left = r"(?<![a-z0-9.])" if strict else r"(?<![0-9.])"
        body = " ?".join(re.escape(p) for p in ps)
        patterns.append((re.compile(left + body), kind, "".join(ps)))

    add(parts, "main")
    has_digit = any(p[0].isdigit() for p in parts)
    if len(parts) >= 2 and parts[-1].isalpha() and has_digit:
        base = parts[:-1]
        if len("".join(base)) >= 5:
            add(base, "base")
    if len(parts) >= 4 and parts[0].isalpha() and len("".join(parts[1:])) >= 5:
        add(parts[1:], "lead")
    if len(parts) == 2 and parts[0] == "f" and parts[1][0].isdigit():
        add(parts[1:], "bare")
    return _Code(token, tuple(patterns))


def _code_bounded(kind: str, code: str, text: str, match: re.Match) -> bool:
    start, end = match.start(), match.end()
    if kind == "main" and code[0].isdigit() and start and text[start - 1].isalpha():
        # Glued after a word ("iphone13", "rtx4070"), not after a short code
        # prefix of its own ("A13" is not the 13).
        j = start
        while j and text[j - 1].isalpha():
            j -= 1
        if start - j < 3 or (j and text[j - 1].isdigit()):
            return False
    rest = text[end:end + 12]
    if not rest or rest[0] == " ":
        return True
    if kind in ("base", "exact") or not rest[0].isalnum():
        return False
    run = re.match(r"[a-z]+|\d+", rest).group(0)
    last_is_digit = code[-1].isdigit()
    if run[0].isdigit() == last_is_digit:
        # Same character class continues the code: X100V is not X100VI, 13 is
        # not 130. Long structured codes carry numeric region suffixes
        # (OLED55C3 -> OLED55C36LC).
        mixed = any(c.isalpha() for c in code) and any(c.isdigit() for c in code)
        return last_is_digit and mixed and len(code) >= 6
    if run in QUALIFIERS or run in _GEN_ROMAN:
        return True  # "13Pro", "A7III": pinned by their own rules
    # A letter suffix on a long code is a sub-variant (DCD796N, 116610LN); on a
    # short one it is a different model (A7R, 4070Ti).
    return last_is_digit and len(code) >= 5


def _find_code(code: _Code, doc: _Doc) -> tuple[int, int] | None:
    """(first, last) token index of the code in ``doc``, or None."""
    text = doc.text
    for pattern, kind, joined in code.patterns:
        for match in pattern.finditer(text):
            if _code_bounded(kind, joined, text, match):
                return text.count(" ", 0, match.start()), text.count(" ", 0, match.end())
    return None


# ---------------------------------------------------------------------------
# Query compilation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Spec:
    tokens: tuple[str, ...]
    token_set: frozenset
    stems: frozenset
    content: tuple[str, ...]
    codes: tuple[_Code, ...]
    split_map: dict
    join_set: frozenset
    qualifiers: frozenset
    generations: frozenset
    series: frozenset
    capacities: frozenset
    colours: frozenset
    volumes_ml: tuple
    grades: frozenset
    sizes: frozenset
    segs: frozenset
    measures: dict
    slots: tuple
    damaged_ok: bool
    lot_qty: int | None
    excludes: frozenset
    allow_lots: bool
    norm: str


@functools.lru_cache(maxsize=256)
def _compile(query: str, extra_excludes: tuple[str, ...], allow_lots: bool) -> _Spec:
    doc = _title_doc(query or "")
    tokens = doc.tokens
    segs = [seg for token in tokens for seg in _segments(token)]
    seg_text = " ".join(segs)
    stems = set()
    for piece in (*tokens, *segs):
        stems |= _stems(piece)
    # Numbers that belong to a grade / size, a unit, a capacity or a volume are
    # pinned by their own rules, not as identity codes.
    attached = set()
    for i, seg in enumerate(segs[:-1]):
        if seg in _GRADERS or seg in _SIZE_SYSTEMS or seg == "size":
            attached.add(segs[i + 1])
    for match in _GRADE_PAIR_RE.finditer(seg_text):
        attached.add(match.group(2))
    codes = []
    for token in dict.fromkeys(tokens):
        if not any(ch.isdigit() for ch in token) or token in STOPWORDS:
            continue
        if (
            _DISCRIM_SKIP_RE.match(token) or _MARK_TOKEN_RE.match(token)
            or token in attached or token.endswith("%") or _VOLUME_RE.fullmatch(token)
        ):
            continue
        codes.append(_code(token))
    markers = _COMPAT_MARKERS | _EXTRAS_MARKERS
    content = tuple(dict.fromkeys(
        t for t in tokens
        if t not in STOPWORDS and t not in markers and len(t) >= 2 and t[:1].isalnum()
    ))
    split_map = {
        a + b: (a, b)
        for a, b in itertools.pairwise(tokens)
        if a.isalpha() and b.isalpha() and (a + b) not in tokens
    }
    slots = tuple(
        (tokens[i - 1], token, tokens[i + 1])
        for i, token in enumerate(tokens[1:-1], start=1)
        if token.isalpha() and 2 <= len(token) <= 3 and token not in STOPWORDS
        and token not in QUALIFIERS and token not in COLOURS
    )
    excludes = set()
    for term in extra_excludes:
        excludes.update(tokenize(term))
    return _Spec(
        tokens=tokens,
        token_set=frozenset(tokens),
        stems=frozenset(stems),
        content=content,
        codes=tuple(codes),
        split_map=split_map,
        join_set=frozenset(t for t in tokens if t.isalpha() and len(t) >= 5),
        qualifiers=_qualifiers(segs),
        generations=_generations(segs),
        series=_series(segs),
        capacities=doc.capacities,
        colours=doc.colours,
        volumes_ml=doc.volumes_ml,
        grades=doc.grades,
        sizes=doc.sizes,
        segs=frozenset(segs),
        measures=_measures(tokens),
        slots=slots,
        damaged_ok=any(hit[0] == "damage" for hit in doc.hits),
        lot_qty=doc.lot_qty,
        excludes=frozenset(excludes),
        allow_lots=allow_lots,
        norm=normalize(canonicalize(query or "")),
    )


def accepts_damaged(query: str) -> bool:
    """Whether the query itself asks for faulty / spares / for-parts units.

    A caller can then keep "for parts" condition listings for such a watch.
    """
    return _compile(query or "", (), False).damaged_ok


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Outcome of :func:`match_verdict`: ``reason`` is "ok" or "<rule>:<detail>"."""

    ok: bool
    reason: str


_OK = Verdict(True, "ok")


def _reject(reason: str) -> Verdict:
    return Verdict(False, reason)


def _align(spec: _Spec, doc: _Doc) -> _Doc:
    """Re-split glued words the query spells apart ("AirMax" -> air max) and
    join split words the query writes glued ("power bank" -> powerbank)."""
    tokens = doc.tokens
    out: list[str] = []
    changed = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in spec.split_map:
            out.extend(spec.split_map[token])
            changed = True
            i += 1
            continue
        if i + 1 < len(tokens) and token + tokens[i + 1] in spec.join_set:
            out.append(token + tokens[i + 1])
            changed = True
            i += 2
            continue
        out.append(token)
        i += 1
    return _doc_from_tokens(tuple(out)) if changed else doc


def _query_word(spec: _Spec, token: str) -> bool:
    return not _stems(token).isdisjoint(spec.stems)


def _locate(spec: _Spec, doc: _Doc, code_hits: dict) -> dict[str, int | None]:
    """Title position of each query content word (-1: present, no position)."""
    found: dict[str, int | None] = {}
    for word in spec.content:
        pos: int | None = None
        if word in code_hits:
            hit = code_hits[word]
            pos = hit[0] if hit else None
        elif normalize_capacity(word) and _CAPACITY_RE.fullmatch(word):
            pos = -1 if normalize_capacity(word) in doc.capacities else None
        elif _VOLUME_RE.fullmatch(word):
            want = _volumes_ml(word)
            pos = -1 if want and any(_same_volume(want[0], v) for v in doc.volumes_ml) else None
        else:
            for stem in _stems(word):
                if stem in doc.first_pos:
                    pos = doc.first_pos[stem]
                    break
        found[word] = pos
    return found


def _coverage(spec: _Spec, found: dict) -> float:
    if not spec.content:
        return 1.0
    return sum(1 for pos in found.values() if pos is not None) / len(spec.content)


def _carries(spec: _Spec, doc: _Doc, coverage: float) -> bool:
    """Whether ``doc`` (typically a title's head clause) names the product."""
    hits = {code.token: _find_code(code, doc) for code in spec.codes}
    if spec.codes:
        return all(hits.values())
    return _coverage(spec, _locate(spec, doc, hits)) >= coverage


def _evaluate(
    spec: _Spec, title: str, coverage: float, fuzzy_threshold: float | None, semantic_ok: bool
) -> Verdict:
    doc = _align(spec, _title_doc(title or ""))
    tokens = doc.tokens

    # 1) Terms the watch explicitly excludes.
    for token in tokens:
        if token in spec.excludes and not _query_word(spec, token):
            return _reject(f"exclude:{token}")

    # 2) Listing type: lots, damage, replicas, packaging, parts, media.
    for family, detail, triggers in doc.hits:
        if family == "quantity" and (spec.allow_lots or detail == str(spec.lot_qty)):
            continue
        if family == "damage" and spec.damaged_ok:
            continue
        if triggers and all(_query_word(spec, t) or t in STOPWORDS for t in triggers):
            continue
        return _reject(f"{family}:{detail}")

    # 3) Identity codes, located once and reused by every later rule.
    code_hits = {code.token: _find_code(code, doc) for code in spec.codes}
    for code in spec.codes:
        hit = code_hits[code.token]
        if hit is None:
            return _reject(f"identity:missing {code.token}")
        after = tokens[hit[1] + 1] if hit[1] + 1 < len(tokens) else ""
        if after in _TIER_SUFFIXES and after not in spec.token_set:
            return _reject(f"identity:tier {after}")
    found = _locate(spec, doc, code_hits)
    positions = [pos for pos in found.values() if pos is not None and pos >= 0]
    span = (min(positions), max(positions)) if positions else (len(tokens), -1)

    # 4) Accessories, compatibility phrases and larger host products.
    marker = tokens[doc.compat_at] if doc.compat_at >= 0 else ""
    if doc.compat_at == 0 and marker not in spec.token_set:
        return _reject(f"accessory:{marker}-initial")  # "For Fitbit Charge 2 Band ..."
    if doc.compat_at > 0:
        head = tokens[:doc.compat_at]
        tail_doc = _doc_from_tokens(tokens[doc.compat_at + 1:])
        # Exempt when the query's own head noun is the thing before the marker
        # ("iphone 13 case" vs "Case for iPhone 13") — unless that head carries
        # a model code of its own ("DeWalt DCB184 Battery for DCD796").
        last = spec.content[-1] if spec.content else ""
        own = bool(last) and _in_doc(last, _doc_from_tokens(head)) and not _in_doc(last, tail_doc)
        if (not own or _foreign_code(spec, head)) and not _carries(
            spec, _doc_from_tokens(head), coverage
        ):
            return _reject(f"accessory:{marker}-phrase")
    wholes = [i for i, t in doc.wholes if not _query_word(spec, t)]
    for index, word, triggers in doc.accessories:
        if all(_query_word(spec, t) for t in triggers):
            continue
        if span[0] < index < span[1]:
            continue  # inside the product's own name: "Ravensburger Mount Fuji 1000"
        if index + 1 in wholes:
            continue  # modifier of the head noun after it: "Smart Controller Drone"
        if any(j < index - 1 for j in wholes):
            continue  # in-box contents after the unit: "Console ... Joy-Con Controllers"
        return _reject(f"accessory:{word}")
    for index, word in doc.hosts:
        if index < span[0] and not _query_word(spec, word):
            return _reject(f"contains:{word}")
    if doc.extras_explicit and doc.extras_start < len(tokens):
        head = tokens[:doc.extras_start]
        foreign = _foreign_code(spec, head) or any(j < doc.extras_start for j in wholes)
        if foreign and not _carries(spec, _doc_from_tokens(head), 1.0):
            return _reject("contains:with-phrase")
    listed = _listed_sibling(doc, code_hits)
    if listed:
        return _reject(f"accessory:fits {listed}")  # "Galaxy S6 S7 S8 S9 Headphones"

    # 5) Identity words: generation, series letter, model line, added tiers.
    generations = _generations(doc.head_segs, spec.segs)
    if spec.generations != generations:
        return _reject(f"identity:generation {sorted(generations) or 'none'}")
    if spec.series != doc.series:
        return _reject("identity:series")
    if spec.qualifiers and spec.qualifiers != doc.qualifiers:
        return _reject(f"identity:qualifier {_qualifier_label(set(doc.qualifiers))}")
    verdict = _added_identity(spec, doc)
    if verdict is not None:
        return verdict

    # 6) Variant attributes the query pins.
    if spec.capacities and not spec.capacities <= _capacities(doc.text, storage_only=False):
        return _reject("variant:capacity")
    for unit, values in spec.measures.items():
        have = _measures(tokens).get(unit)
        if have and not values & have:
            return _reject(f"variant:{unit}")  # "45mm" watch vs a "41mm" listing
    if not spec.colours <= doc.colours:
        return _reject("variant:colour")
    for want in spec.volumes_ml:
        if not any(_same_volume(want, have) for have in doc.volumes_ml):
            return _reject("variant:volume")
    if not spec.grades <= doc.grades:
        return _reject("variant:grade")
    for system, size in spec.sizes:
        if not any(
            have == size and (system in (s, "size") or s == "size") for s, have in doc.sizes
        ):
            return _reject("variant:size")

    # 7) Relevance: coverage, then fuzzy ratio, then semantic similarity.
    share = _coverage(spec, found)
    if share >= coverage:
        return _OK
    if fuzzy_threshold is not None and fuzzy_ratio(spec.norm, doc.text) >= fuzzy_threshold:
        return _OK
    if semantic_ok:
        return _OK
    return _reject(f"relevance:coverage {share:.2f}")


_SHAPE_RE = re.compile(r"([a-z]*)(\d{1,3})")


def _listed_sibling(doc: _Doc, code_hits: dict) -> str | None:
    """A sibling model listed right beside the query's code ("S8 S9", "7 8 Plus")
    marks a compatibility list — an accessory that fits several models."""
    tokens = doc.tokens
    for token, hit in code_hits.items():
        shape = _SHAPE_RE.fullmatch(token)
        if not shape or not hit:
            continue
        # A bare number lists forward only ("7 8 Plus"); "Paperwhite 5 11" is a
        # second name for the same generation.
        sides = (hit[0] - 1, hit[1] + 1) if shape.group(1) else (hit[1] + 1,)
        for j in sides:
            if not 0 <= j < len(tokens) or tokens[j] == token:
                continue
            other = _SHAPE_RE.fullmatch(tokens[j])
            if not other or other.group(1) != shape.group(1):
                continue
            if abs(len(other.group(2)) - len(shape.group(2))) > 1:
                continue
            after = tokens[j + 1] if j + 1 < len(tokens) else ""
            # A bare number is a list member only when nothing it could count
            # follows ("7 8 Plus", not "7 2 Year Warranty").
            if not shape.group(1) and not (
                after == "" or after in QUALIFIERS or _SHAPE_RE.fullmatch(after)
                or not after[:1].isalnum()
            ):
                continue
            return tokens[j]
    return None


def _foreign_code(spec: _Spec, tokens: Sequence[str]) -> bool:
    """Whether ``tokens`` carry a model code the query does not name."""
    return any(
        any(ch.isdigit() for ch in t) and t[:1].isalnum() and not _DISCRIM_SKIP_RE.match(t)
        and not _query_word(spec, t)
        for t in tokens
    )


def _in_doc(token: str, doc: _Doc) -> bool:
    return any(stem in doc.first_pos for stem in _stems(token))


def _added_identity(spec: _Spec, doc: _Doc) -> Verdict | None:
    """Words a title *adds* that name a different product than the query."""
    tokens = doc.tokens
    head = tokens[:doc.extras_start]
    for i, token in enumerate(head):
        if _query_word(spec, token):
            continue
        nxt = head[i + 1] if i + 1 < len(head) else ""
        if token.isdigit() and nxt in ("edition", "ed") and token not in spec.token_set:
            return _reject(f"identity:edition {token}")
        if _in(token, _AUDIENCE) and nxt not in ("edition", "version"):
            return _reject(f"identity:audience {token}")
        if token in _LANGUAGES:
            return _reject(f"identity:language {token}")
    for left, word, right in spec.slots:
        for i in range(len(head) - 2):
            other = head[i + 1]
            if (
                head[i] == left and head[i + 2] == right and other != word
                and other.isalpha() and 2 <= len(other) <= 3 and other not in spec.token_set
            ):
                return _reject(f"identity:slot {other}")
    return None


def generation_of(query: str, title: str) -> str:
    """The generation the title states for the product the query names.

    A digit string — "3" for "Echo Dot (3rd Gen)", "6" for "Fenix 6S Pro", "2"
    for "Versa 2", "AirPods 2nd Generation" or "R6 Mark II" — or "base" when
    the title states none. Lets pricing cluster the generations that an
    unpinned query ("echo dot") accepts.
    """
    spec = _compile(query or "", (), False)
    doc = _align(spec, _title_doc(title or ""))
    head = doc.tokens[:doc.extras_start]
    for i, token in enumerate(head[:-1]):
        if not (token.isalpha() and _query_word(spec, token)) or token in QUALIFIERS:
            continue
        first = _segments(head[i + 1])[0]
        after = head[i + 2] if i + 2 < len(head) else ""
        if first.isdigit() and 1 <= int(first) <= 20 and not (
            _is_plural(after) or after in _QTY_NOUNS
        ):
            return str(int(first))
    for i, token in enumerate(head):
        if token in ("gen", "generation"):
            for j in (i - 1, i + 1):
                if 0 <= j < len(head) and head[j].isdigit() and 1 <= int(head[j]) <= 20:
                    return str(int(head[j]))
    generations = _generations(doc.head_segs, spec.segs)
    return min(generations, key=int) if generations else "base"


def match_verdict(
    query: str,
    title: str,
    *,
    extra_excludes: Iterable[str] = (),
    coverage: float = DEFAULT_COVERAGE,
    fuzzy_threshold: float | None = None,
    semantic_ok: bool = False,
    allow_lots: bool = False,
) -> Verdict:
    """Whether ``title`` is the product ``query`` names, and if not, why.

    ``reason`` is "ok" or a stable "<rule>:<detail>" code for the first rule
    that rejected, e.g. "accessory:for-phrase", "identity:missing 13",
    "damage:faulty", "quantity:2", "variant:capacity", "relevance:coverage 0.50".
    Guardrails can never be overridden; ``semantic_ok`` only rescues relevance.
    """
    spec = _compile(query or "", tuple(extra_excludes), allow_lots)
    return _evaluate(spec, title, coverage, fuzzy_threshold, semantic_ok)


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
    """Whether ``title`` is the same product as ``query`` (see match_verdict)."""
    return match_verdict(
        query,
        title,
        extra_excludes=extra_excludes,
        coverage=coverage,
        fuzzy_threshold=fuzzy_threshold,
        semantic_ok=semantic_ok,
        allow_lots=allow_lots,
    ).ok


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

    The query is compiled once. Semantic similarity is computed lazily, only
    for titles that pass every guardrail but fail lexical relevance.
    """
    spec = _compile(query or "", tuple(extra_excludes), allow_lots)
    titles = [key(item) for item in items]
    verdicts = [_evaluate(spec, title, coverage, fuzzy_threshold, False) for title in titles]
    keep = [verdict.ok for verdict in verdicts]
    if semantic_threshold is not None:
        pending = [i for i, v in enumerate(verdicts) if v.reason.startswith("relevance")]
        if pending:
            sims = semantic.similarities(
                canonicalize(query), [canonicalize(titles[i]) for i in pending]
            )
            if sims is not None:
                for i, similarity in zip(pending, sims):
                    keep[i] = similarity >= semantic_threshold
    return [item for item, kept in zip(items, keep) if kept]
