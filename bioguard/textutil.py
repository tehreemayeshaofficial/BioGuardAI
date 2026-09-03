"""Text-normalisation helpers shared by the parsers and matching engines.

Laboratory exports are messy: organisms arrive as ``"E. coli"``, ``"E.coli"``,
``"ESCHERICHIA COLI (ESBL)"``; wards as ``"icu"``, ``"ICU-1"``,
``"Intensive Care Unit"``. Every matcher in Bioguard reduces its input through
:func:`normalise` first, so alias tables can be written in a single canonical
form.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(r"\s+")

# Tokens carrying no identification value in an organism name.
ORGANISM_STOPWORDS = {
    "spp", "sp", "species", "gen", "genus", "sp1", "sp2", "isolate", "isolates",
    "strain", "unknown", "n", "na", "nt", "un", "undetermined", "aureus",
}

# Bacteria are never these words; used to avoid false antibiotic hits.
NOISE_TOKENS = {
    "date", "time", "result", "value", "test", "lab", "report", "specimen",
    "susceptible", "resistant", "intermediate", "mic", "zone", "mm", "ug", "ml",
}


def to_ascii(text: str) -> str:
    """Strip accents/diacritics (``Klebsiella pneumoniaeë`` -> plain ASCII)."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise(text: str) -> str:
    """Canonical comparison form: lowercase, ASCII, alphanumeric + single spaces.

    ``"  Staphylococcus  AUREUS (MRSA)! "`` -> ``"staphylococcus aureus mrsa"``
    """
    if text is None:
        return ""
    s = to_ascii(text).lower().replace("&", " and ")
    s = _NON_ALNUM.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def tokens(text: str) -> list[str]:
    """Normalised word list."""
    return [t for t in normalise(text).split(" ") if t]


def compact(text: str) -> str:
    """Normalised text with all spaces removed (catches ``"e coli"`` vs ``"ecoli"``)."""
    return normalise(text).replace(" ", "")


def collapse_ws(text: str) -> str:
    return _SPACES.sub(" ", (text or "").strip())


def titlecase_organism(text: str) -> str:
    """Display an organism name in conventional binomial italics-friendly case."""
    s = collapse_ws(to_ascii(text or ""))
    if not s:
        return ""
    parts = s.split(" ")
    parts = [p[0].upper() + p[1:].lower() if p else p for p in parts]
    return " ".join(parts)


def phrase_in(phrase: str, haystack: str) -> bool:
    """Word-boundary containment test between two *already normalised* strings."""
    if not phrase or not haystack:
        return False
    pattern = r"(?:^|[\s\-_/,;.:])(?:" + re.escape(phrase) + r")(?:$|[\s\-_/,;.:])"
    return re.search(pattern, haystack) is not None


def looks_like_header(value: str) -> bool:
    """True when a CSV cell is a column name rather than data."""
    v = (value or "").strip()
    return bool(v) and v.lower() in {"nan", "none", "null", "na", "n/a", "-", ""}


def clean_cell(value) -> str:
    """Coerce arbitrary CSV/PDF cell content to a tidy string."""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and value != value:  # NaN
            return ""
    except Exception:
        pass
    s = str(value)
    s = s.replace("\r", " ").replace("\x00", " ")
    s = collapse_ws(s.replace("\n", " "))
    if s.strip().lower() in {"nan", "none", "null", "nat"}:
        return ""
    return s.strip()


def human_int(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
