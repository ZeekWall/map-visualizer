"""Normalizes punctuation variants that commonly differ between OSM's
community-edited tags and All The Places' scraped brand names -- a straight
apostrophe (') vs a typographic one (’) in "McDonald's"/"McDonald's", or a
hyphen vs an en-dash, is the same real-world brand, but every exact-match
comparison in this codebase (ATP's `brand not in atp_brands` allow-list
filter, an Overpass `brand~"^...$"` regex built from whatever the user
typed) treats them as different strings and silently drops one side.

Deliberately narrow: only touches punctuation classes known to vary
source-to-source for the same brand, not a general fuzzy-matching or
transliteration pass -- diacritics, casing, etc. are handled elsewhere
(regex `,i` flags, `.lower()`) or not at all.
"""

import re

_QUOTE_CHARS = "'‘’ʼ´`"
_DASH_CHARS = "-–—−"

_NORMALIZE_MAP = {ch: "'" for ch in _QUOTE_CHARS}
_NORMALIZE_MAP.update({ch: "-" for ch in _DASH_CHARS})
_NORMALIZE_MAP["\u00a0"] = " "  # non-breaking space
_NORMALIZE_RE = re.compile("[" + re.escape("".join(_NORMALIZE_MAP)) + "]")
_QUOTE_RE = re.compile("[" + re.escape(_QUOTE_CHARS) + "]")


def normalize(s):
    """Canonicalize punctuation variants for comparison -- e.g. for an
    equality/set-membership check like ATP's brand allow-list filter. Not
    for display or for writing into targets.py: callers should match on
    this, but keep showing/storing the user's/data's original spelling.
    """
    return _NORMALIZE_RE.sub(lambda m: _NORMALIZE_MAP[m.group(0)], s)


def fuzzy_regex(s):
    """Escape `s` for use inside an Overpass/PCRE regex, but widen any
    apostrophe/quote or dash character to a class matching every common
    variant -- a `^...$` match built from exactly what the user typed
    silently misses the other spelling when OSM's tag disagrees with it.
    """
    out = []
    for ch in s:
        if ch in _QUOTE_CHARS:
            out.append("[" + re.escape(_QUOTE_CHARS) + "]")
        elif ch in _DASH_CHARS:
            out.append("[" + re.escape(_DASH_CHARS) + "]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def strip_apostrophes(s):
    """Delete any apostrophe/quote-variant character entirely -- no
    underscore, no space, nothing in its place.

    Unlike normalize()/fuzzy_regex() above (which treat every apostrophe
    style as equivalent punctuation), some naming conventions drop it
    outright instead of treating it as a separator: All The Places slugs
    "Raising Cane's" to "raising_canes_us", not "raising_cane_s_us".
    Collapsing the apostrophe to "_" like every other separator inserts a
    word boundary that was never there and breaks the match. Used when
    building that kind of slug/token for comparison, not for regex/set
    matching against already-tagged data.
    """
    return _QUOTE_RE.sub("", s)


def merge_variants(counts_by_raw):
    """counts_by_raw: dict[str, int] keyed by raw observed value (e.g. a
    tallied `brand=` value). Merges keys that normalize to the same
    canonical form, keeping the most-frequent raw spelling as the display
    key and summing counts -- so a brand split across two near-identical
    spellings in the source data shows up as one row with the true total,
    and selecting it in the UI captures every real-world variant (the
    normalize()-aware filters above will match either spelling anyway, but
    this keeps the counts/checkboxes from fragmenting in the first place).
    """
    groups = {}
    for raw, count in counts_by_raw.items():
        key = normalize(raw)
        bucket = groups.setdefault(key, {})
        bucket[raw] = bucket.get(raw, 0) + count
    merged = {}
    for variants in groups.values():
        best_raw = max(variants, key=variants.get)
        merged[best_raw] = sum(variants.values())
    return merged
