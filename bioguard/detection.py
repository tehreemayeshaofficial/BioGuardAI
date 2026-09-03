"""
Organism identification engine.

Turns whatever a laboratory wrote ("ESCHERICHIA COLI (ESBL)", "Klebsiella sp.",
"CoNS", "Staphylococcus aureus - oxacillin R") into one of the twelve tracked
targets, or a deliberate "not a target" decision, with an auditable confidence
score and the exact phrase that produced the verdict.

Matching is deliberately conservative: an organism is only promoted to a target
when a target-specific phrase matches, and a more specific non-target phrase
(e.g. *Klebsiella oxytoca*) always beats a broader target phrase
(e.g. ``klebsiella``).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .textutil import clean_cell, compact, normalise, phrase_in, titlecase_organism
from .pathogens import (MARKER_SYNONYMS, NON_TARGET_ORGANISMS, PATHOGENS,
                        RESISTANCE_MARKERS, display_name)

# --------------------------------------------------------------------------
# Static indices, built once at import time
# --------------------------------------------------------------------------
_TARGET_INDEX: list[tuple[str, str, bool, float]] = []  # (alias, key, genus_level, confidence)
_NONTARGET_INDEX: list[tuple[str, str]] = []
_FUZZY_CORPUS: dict[str, str] = {}
_GENUS_OF: dict[str, str] = {}          # genus word -> pathogen key
_MARKER_PHRASES: list[tuple[str, str]] = []
_BUILD_OK = False

# A genus-only report ("Salmonella spp.") is a complete answer for these
# catalogues because the target itself is defined at genus level.
GENUS_LEVEL_TARGETS = {"salmonella", "enterococcus", "streptococcus"}

_NEGATION_RE = re.compile(
    r"\b(no growth|not detected|not isolated|not found|absent|negative for|"
    r"n\[/ ?a\]|no pathogen|no organism|screen negative|nil seen|"
    r"no evidence of|culture negative|unsuitable)\b")


@dataclass
class OrganismCall:
    """Result of identifying one organism string."""
    raw: str = ""
    pathogen: str | None = None
    confidence: float = 0.0
    method: str = "none"          # alias | genus | compact | fuzzy | phenotype | nontarget | none
    matched: str = ""
    other_label: str = ""         # human-readable label for non-target organisms
    markers: list[str] = field(default_factory=list)
    genus_only: bool = False
    suppressed: bool = False      # "no growth of X" - identified but not a case
    result_hint: str = ""         # positive | negative | ''

    @property
    def is_target(self) -> bool:
        return bool(self.pathogen) and self.pathogen in PATHOGENS

    @property
    def counts_as_case(self) -> bool:
        return self.is_target and not self.suppressed

    @property
    def pathogen_display(self) -> str:
        if self.pathogen:
            return display_name(self.pathogen)
        return self.other_label or "Unidentified organism"

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "pathogen": self.pathogen,
            "pathogen_display": self.pathogen_display,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "matched": self.matched,
            "other_label": self.other_label,
            "markers": self.markers,
            "genus_only": self.genus_only,
            "suppressed": self.suppressed,
            "is_target": self.is_target,
        }


def _forms_for(key: str) -> set[str]:
    p = PATHOGENS[key]
    forms = {normalise(a) for a in p.aliases}
    forms |= {normalise(p.name), normalise(p.short_name), normalise(p.key)}
    return {f for f in forms if len(f) >= 3}


def _build_indices() -> None:
    global _BUILD_OK
    if _BUILD_OK:
        return
    markers = [(normalise(k), k.upper()) for k in RESISTANCE_MARKERS]
    markers += [(normalise(k), v) for k, v in MARKER_SYNONYMS.items()]
    _MARKER_PHRASES.extend(sorted(markers, key=lambda t: -len(t[0])))

    for key, p in PATHOGENS.items():
        genus = normalise(p.name).split(" ")[0]
        _GENUS_OF.setdefault(genus, key)

    for key in PATHOGENS:
        genus = normalise(PATHOGENS[key].name).split(" ")[0]
        genus_is_target = key in GENUS_LEVEL_TARGETS
        for f in _forms_for(key):
            genus_level = ((" " not in f and f == genus)
                           or f.endswith(" spp") or f.endswith(" sp"))
            if genus_level:
                # "Salmonella spp." is a complete answer; "Klebsiella spp." is not.
                conf = 0.95 if genus_is_target else 0.65
            else:
                conf = 0.99
            _TARGET_INDEX.append((f, key, genus_level, conf))
            _FUZZY_CORPUS.setdefault(f, key)

    _TARGET_INDEX.sort(key=lambda t: -len(t[0]))

    for alias, label in NON_TARGET_ORGANISMS.items():
        n = normalise(alias)
        if len(n) >= 3:
            _NONTARGET_INDEX.append((n, label))
    _NONTARGET_INDEX.sort(key=lambda t: -len(t[0]))

    for p in PATHOGENS.values():
        for extra in (normalise(p.name), normalise(p.short_name)):
            _FUZZY_CORPUS.setdefault(extra, p.key)
    _BUILD_OK = True


_build_indices()


# --------------------------------------------------------------------------
# Marker scanning
# --------------------------------------------------------------------------
def scan_markers(normalised_text: str) -> list[str]:
    """Pull resistance phenotype tags (MRSA / ESBL / VRE / CRE ...) out of text."""
    found: list[str] = []
    for phrase, canon in _MARKER_PHRASES:
        if canon in found:
            continue
        if phrase_in(phrase, normalised_text):
            found.append(canon)
    return found


def _best_phrase(query: str, ranked: list) -> tuple | None:
    """First (longest) phrase occurring as a word-boundary phrase in *query*."""
    for entry in ranked:
        alias = entry[0]
        if alias in query and phrase_in(alias, query):
            return entry
    return None


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def identify_organism(raw: str) -> OrganismCall:
    _build_indices()
    text = clean_cell(raw)
    call = OrganismCall(raw=text)
    n = normalise(text)
    if not n:
        return call

    call.markers = scan_markers(n)
    negated = bool(_NEGATION_RE.search(n))
    call.result_hint = "negative" if negated else "positive"

    best_target = _best_phrase(n, _TARGET_INDEX)
    best_other = _best_phrase(n, _NONTARGET_INDEX)

    if best_target and best_other and len(best_other[0]) > len(best_target[0]):
        # e.g. "Klebsiella oxytoca" beats the broader "klebsiella" genus alias
        call.method = "nontarget"
        call.other_label = best_other[1]
        call.confidence = 0.95
        call.suppressed = negated
        return call

    if best_other and not best_target:
        call.method = "nontarget"
        call.other_label = best_other[1]
        call.confidence = 0.95
        return call

    if best_target:
        alias, key, genus_level, conf = best_target
        call.pathogen = key
        call.matched = alias
        call.genus_only = genus_level
        call.confidence = conf
        call.method = "genus" if genus_level else "alias"
        # Explicit phenotype tag in the label outranks the species wording.
        if "MRSA" in call.markers and key == "staph_aureus":
            call.pathogen = "mrsa"
            call.method = "phenotype"
            call.confidence = 0.99
        call.suppressed = negated
        return call

    # --- space-insensitive pass: "E.coli" / "ecoli" / "K.pneumoniae" -----
    c = compact(text)
    if len(c) >= 5:
        best_compact: tuple[str, str] | None = None
        for alias, key, _gl, _conf in _TARGET_INDEX:
            ca = alias.replace(" ", "")
            if len(ca) >= 5 and ca in c:
                if best_compact is None or len(ca) > len(best_compact[0]):
                    best_compact = (ca, key)
        if best_compact:
            call.pathogen = best_compact[1]
            call.matched = best_compact[0]
            call.method = "compact"
            call.confidence = 0.9
            call.suppressed = negated
            return call

    # --- fuzzy fallback for typos / OCR damage --------------------------
    if len(n) >= 5:
        for cand in difflib.get_close_matches(n, list(_FUZZY_CORPUS), n=3, cutoff=0.0):
            ratio = difflib.SequenceMatcher(None, n, cand).ratio()
            if ratio >= 0.88:
                call.pathogen = _FUZZY_CORPUS[cand]
                call.matched = cand
                call.method = "fuzzy"
                call.confidence = round(min(0.85, ratio * 0.9), 3)
                call.suppressed = negated
                return call

    call.method = "none"
    call.confidence = 0.0
    call.other_label = f"Unmapped: {titlecase_organism(text)}"
    return call


# --------------------------------------------------------------------------
# Phenotype upgrades that require the susceptibility panel
# --------------------------------------------------------------------------
_ANTI_STAPHYL_PENICILLINS = {
    "oxacillin", "cefoxitin", "methicillin", "cloxacillin", "flucloxacillin",
    "nafcillin",
}


def apply_phenotype_upgrades(call: OrganismCall, sensitivities) -> OrganismCall:
    """Reclassify using the antibiogram.

    S. aureus with a non-susceptible oxacillin/cefoxitin result is MRSA - the
    single most common real-world lab-reporting inconsistency.
    """
    def get(obj, attr):
        if isinstance(obj, dict):
            return obj.get(attr)
        return getattr(obj, attr, None)

    if call.pathogen == "staph_aureus":
        for s in sensitivities or []:
            ab = get(s, "antibiotic")
            res = (get(s, "result") or "").upper()
            if ab in _ANTI_STAPHYL_PENICILLINS and res == "R":
                call.pathogen = "mrsa"
                call.method = "phenotype"
                call.matched = f"{ab}=R"
                call.confidence = max(call.confidence, 0.95)
                call.genus_only = False
                if "MRSA" not in call.markers:
                    call.markers.append("MRSA")
                break
    return call


# --------------------------------------------------------------------------
# Ward / location handling
# --------------------------------------------------------------------------
_WARD_MAP = {
    "intensive care unit": "ICU", "icu general": "ICU", "general icu": "ICU",
    "intensive care": "ICU", "critical care": "ICU", "ccu": "CCU",
    "medical intensive care unit": "MICU", "surgical intensive care unit": "SICU",
    "neonatal intensive care unit": "NICU", "paediatric intensive care unit": "PICU",
    "hdu": "HDU", "high dependency unit": "HDU",
    "emergency department": "ED", "accident and emergency": "ED", "casualty": "ED",
    "er": "ED", "emergency room": "ED",
    "operating theatre": "Theatre", "theatre": "Theatre", "ot": "Theatre",
    "neonatal unit": "Neonatal Unit", "special care baby unit": "SCBU",
    "neonatal intensive care": "NICU",
    "haematology oncology": "Haematology-Oncology", "oncology": "Oncology",
    "haematology": "Haematology", "hematology": "Haematology",
    "renal dialysis": "Dialysis Unit", "dialysis": "Dialysis Unit",
    "renal unit": "Dialysis Unit", "burn unit": "Burns Unit", "burns": "Burns Unit",
    "transplant unit": "Transplant Unit", "transplant": "Transplant Unit",
    "long term acute care": "LTAC", "geriatric ward": "Geriatrics",
    "nursing home": "Nursing Home", "residential care": "Nursing Home",
    "outpatient": "Outpatient", "ambulatory": "Outpatient",
    "maternity": "Maternity", "obstetrics": "Maternity", "labour ward": "Maternity",
    "paediatrics": "Paediatrics", "pediatrics": "Paediatrics",
    "childrens ward": "Paediatrics", "general surgery": "General Surgery",
    "general medicine": "General Medicine", "infectious diseases": "Infectious Diseases",
    "isolation unit": "Isolation Unit", "sterile unit": "Protective Environment",
}

_WARD_CODE_RE = re.compile(r"^(icu|nicu|picu|nsicu|hdu|ccu|sicu|micu|ed)\b[\s_-]*(\d*[a-z]?)$", re.I)


def normalise_ward(raw: str) -> str:
    """Canonicalise a ward/location label so that grouping is meaningful."""
    text = clean_cell(raw)
    if not text:
        return "Unspecified"
    n = normalise(text)
    if not n:
        return "Unspecified"

    if n in _WARD_MAP:
        return _WARD_MAP[n]

    m = _WARD_CODE_RE.match(n)
    if m:
        code, num = m.group(1).upper(), (m.group(2) or "").strip().upper()
        return f"{code} {num}".strip()

    m = re.match(r"^ward[\s_-]*([a-z0-9]+)$", n)
    if m:
        return f"Ward {m.group(1).upper()}"

    m = re.match(r"^(bed|room|bay)[\s_-]*([a-z0-9]+)$", n)
    if m:
        return f"{m.group(1).title()} {m.group(2).upper()}"

    for key in sorted(_WARD_MAP, key=len, reverse=True):
        if len(key) > 5 and phrase_in(key, n):
            suffix = re.search(r"(\d+[a-z]?)$", n)
            return _WARD_MAP[key] + (f" {suffix.group(1).upper()}" if suffix else "")

    out = re.sub(r"\bward\b\s+(\w)", lambda mm: "Ward " + mm.group(1).upper(), n)
    out = titlecase_organism(out)
    return re.sub(r"\s+", " ", out).strip()[:60] or "Unspecified"


_SPECIMEN_MAP = {
    "blood culture": "Blood", "blood": "Blood",
    "urine": "Urine", "midstream urine": "Urine", "msu": "Urine",
    "catheter urine": "Urine (catheter)", "catheter specimen of urine": "Urine (catheter)",
    "sputum": "Sputum", "endotracheal aspirate": "ETA", "eta": "ETA",
    "bronchoalveolar lavage": "BAL", "bal": "BAL",
    "stool": "Stool", "faeces": "Stool", "feces": "Stool",
    "rectal swab": "Rectal swab", "rectal": "Rectal swab", "perianal": "Rectal swab",
    "high vaginal swab": "HVS", "hvs": "HVS",
    "throat swab": "Throat swab", "throat": "Throat swab", "swab throat": "Throat swab",
    "nasal swab": "Nasal swab", "nose": "Nasal swab", "nasal": "Nasal swab",
    "nares": "Nasal swab", "axillary swab": "Axillary swab", "groin swab": "Groin swab",
    "wound": "Wound", "wound swab": "Wound", "tissue": "Tissue", "biopsy": "Tissue",
    "csf": "CSF", "cerebrospinal fluid": "CSF", "lumbar puncture": "CSF",
    "pleural fluid": "Pleural fluid", "ascitic fluid": "Ascitic fluid",
    "peritoneal fluid": "Peritoneal fluid", "joint fluid": "Joint fluid",
    "synovial fluid": "Joint fluid", "catheter tip": "Catheter tip",
    "line tip": "Catheter tip", "central venous catheter": "Catheter tip",
    "sputum brush": "Sputum", "urine cath": "Urine (catheter)",
    "environmental swab": "Environmental", "surface swab": "Environmental",
}


def normalise_specimen(raw: str) -> str:
    text = clean_cell(raw)
    if not text:
        return "Unspecified"
    n = normalise(text)
    if n in _SPECIMEN_MAP:
        return _SPECIMEN_MAP[n]
    for key in sorted(_SPECIMEN_MAP, key=len, reverse=True):
        if len(key) >= 4 and phrase_in(key, n):
            return _SPECIMEN_MAP[key]
    return titlecase_organism(text)[:40]


# --------------------------------------------------------------------------
# Patient identity
# --------------------------------------------------------------------------
def normalise_patient(raw: str) -> str:
    """Stable patient key. Lab exports mix 'P-0041', '41' and 'MRN 00041'."""
    text = clean_cell(raw)
    if not text:
        return "UNKNOWN"
    n = normalise(text)
    m = re.match(r"^(?:mrn|patient|pt|case|hospital|accession|acc|id)[\s_-]*(\w+)$", n)
    if m:
        text, n = m.group(1), normalise(m.group(1))
    # Zero-padded numeric IDs: '00041' and '41' are the same person.
    if re.fullmatch(r"\d+", n):
        return f"P{int(n):06d}"
    return re.sub(r"\s+", "", text).upper()[:32]
