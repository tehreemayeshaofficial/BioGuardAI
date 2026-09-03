"""Shared parsing primitives: header mapping, dates, susceptibility codes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..antibiotics import category_of, display_name, resolve_antibiotic
from ..detection import (apply_phenotype_upgrades, identify_organism,
                         normalise_patient, normalise_specimen, normalise_ward)
from ..textutil import clean_cell, normalise

# --------------------------------------------------------------------------
# Column header vocabulary
# --------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "patient_id": ("patient id", "patient", "patient code", "patient number", "mrn",
                   "medical record number", "hospital number", "hos num", "case id",
                   "case number", "patient identifier", "subject id", "pid", "pat id",
                   "patient account number", "account number", "emri", "chip id",
                   "unique patient id", "patient ref", "ref", "reference"),
    "patient_name": ("patient name", "name", "full name", "patient surname", "surname",
                     "nom", "patient fullname", "client name"),
    "date_of_birth": ("dob", "date of birth", "birth date", "birthdate"),
    "age": ("age", "patient age"),
    "sex": ("sex", "gender"),
    "ward": ("ward", "unit", "care unit", "location", "current location", "site",
             "ward name", "nursing unit", "department", "clinic", "area", "zone",
             "building", "home unit", "admitting ward", "ward unit", "placed in"),
    "room": ("room", "bed", "bay", "room number", "bed number", "room bed",
             "house block", "room bay"),
    "specimen_type": ("specimen", "specimen type", "sample type", "specimen source",
                      "source", "material", "specimen description", "sample",
                      "type of specimen", "isolate source", "germ source", "matrix"),
    "sample_date": ("sample date", "date collected", "collection date", "collected",
                    "date of collection", "specimen date", "date", "sampled on",
                    "sampled", "date sampled", "sample date time", "collected on",
                    "draw date", "draw date time", "collection date time",
                    "test date", "specimen collected"),
    "received_date": ("received", "date received", "received date", "accession date"),
    "result_date": ("result date", "reported", "report date", "released", "finalized",
                    "result date time", "reported on"),
    "organism": ("organism", "pathogen", "bacteria", "isolate", "agent", "species",
                 "micro organism", "microorganism", "organism identified",
                 "organism name", "identified organism", "germ", "microbe",
                 "target", "germe", "causative agent", "isolated organism",
                 "organism species", "bacterial species", "microbiology"),
    "antibiotic": ("antibiotic", "antimicrobial", "antibiotic name", "drug",
                   "antimicrobial agent", "agent", "substance", "medication",
                   "disk", "disk name", "name of antibiotic", "compound", "abx"),
    "result": ("result", "susceptibility", "sensitivity", "interpretation", "call",
               "sus", "screen result", "result value", "resistance result",
               "phenotype", "final result", "qualitative result"),
    "mic": ("mic", "mic value", "mic ug ml", "minimum inhibitory concentration",
            "mic ug", "mcg ml", "mic result"),
    "zone": ("zone", "zone diameter", "inhibition zone", "diameter", "zone mm"),
    "method": ("method", "technique", "platform", "assay", "methodology", "test method"),
    "resistance_profile": ("resistance profile", "resistance", "profile", "mdr profile",
                           "sensitivities", "susceptibility profile", "antibiogram",
                           "resistance pattern", "amr profile", "panel result"),
    "notes": ("note", "notes", "comment", "comments", "remark", "remarks",
              "interpretation comment", "flag", "critique"),
    "result_flag": ("present", "detected", "positive", "detection", "culture result",
                    "reagent", "presumptive", "qualitative"),
    "clinician": ("clinician", "doctor", "requested by", "physician", "consultant",
                  "referring provider", "consulting physician"),
    "lab_no": ("lab no", "lab number", "accession", "accession number", "specimen id",
               "sample id", "test id", "order number", "lab_no", "specimen number"),
}

_INDEX: dict[str, str] | None = None


def _header_index() -> dict[str, str]:
    """alias -> field, longest alias first so 'date collected' beats 'date'."""
    global _INDEX
    if _INDEX is None:
        idx: dict[str, str] = {}
        for fld, aliases in COLUMN_ALIASES.items():
            for a in aliases:
                idx[normalise(a)] = fld
        for fld in COLUMN_ALIASES:
            idx.setdefault(normalise(fld), fld)
        _INDEX = dict(sorted(idx.items(), key=lambda kv: -len(kv[0])))
    return _INDEX


def map_field(header: str) -> str | None:
    """Resolve a column header to a semantic field name (or None)."""
    n = normalise(header)
    if not n:
        return None
    idx = _header_index()
    if n in idx:
        return idx[n]
    # Headers often carry parenthetical codes: "Ward (LOC)" or "Date Collected *".
    n = re.sub(r"\s*\*+$", "", n)
    if n in idx:
        return idx[n]
    for alias, fld in idx.items():
        if len(alias) >= 5 and n.startswith(alias):
            return fld
    return None


def map_columns(headers: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({field: header}, {antibiotic_field_key: header}).

    The second mapping carries every header that resolved to a canonical
    antibiotic but to no semantic field - i.e. a *wide* susceptibility panel.
    """
    fields: dict[str, str] = {}
    antibiotics: dict[str, str] = {}
    for h in headers:
        raw = clean_cell(h)
        if not raw:
            continue
        fld = map_field(raw)
        if fld and fld not in fields:
            fields[fld] = raw
            continue
        ab = resolve_antibiotic(raw)
        if ab and ab not in antibiotics:
            antibiotics[ab] = raw
    return fields, antibiotics


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y",
    "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y",
    "%Y%m%d", "%d/%m/%y", "%m/%d/%y", "%y-%m-%d", "%d-%m-%y",
)
_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(value) -> str:
    """Best-effort extraction of a calendar date -> ISO ``YYYY-MM-DD`` or ``''``."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = clean_cell(value)
    if not text:
        return ""

    # Numeric Excel serial dates (e.g. 45123.5)
    if re.fullmatch(r"\d{4,5}(?:\.\d+)?", text):
        try:
            serial = float(text)
            if 20000 <= serial <= 60000:
                return (_EXCEL_EPOCH + timedelta(days=int(serial))).isoformat()
        except ValueError:
            pass

    # ISO-ish / natural language with time suffix
    head = re.split(r"[ tT]", text, 1)[0]
    for fmt in _DATE_FORMATS:
        for candidate in (text, head):
            try:
                return datetime.strptime(candidate.strip(), fmt).date().isoformat()
            except ValueError:
                continue

    m = re.search(r"(\d{1,4})\D+(\d{1,2})\D+(\d{1,4})", text)
    if m:
        a, b, c = (int(g) for g in m.groups())
        year, month, day = _resolve_ymd(a, b, c)
        return _safe_date(year, month, day)

    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{2,4})", text)
    if m:
        return _safe_date(_expand_year(m.group(3)), _month_number(m.group(2)),
                          int(m.group(1)))
    return ""


def _resolve_ymd(a: int, b: int, c: int) -> tuple[int, int, int]:
    """Order day/month/year out of three ambiguous integers.

    Four-digit token is the year. Otherwise the >12 token settles the ambiguity;
    when both are <= 12 the day-first convention used by most microbiology
    information systems wins.
    """
    if a >= 1000:
        return a, b, c
    if c < 1000:
        c += 2000 if c < 70 else 1900
    if b > 12:
        return c, a, b        # m/d/Y
    return c, b, a            # default day-first


def _safe_date(year: int, month: int, day: int) -> str:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _expand_year(y: str) -> int:
    n = int(y)
    if n < 100:
        return n + 2000 if n < 70 else n + 1900
    return n


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _month_number(name: str) -> int:
    return _MONTHS.get(name[:3].lower(), 1)


# --------------------------------------------------------------------------
# Susceptibility interpretation
# --------------------------------------------------------------------------
_RESULT_WORDS = {
    "r": "R", "res": "R", "resis": "R", "resistant": "R", "resistence": "R",
    "resistance": "R", "non susceptible": "R", "nonsusceptible": "R",
    "non-susceptible": "R", "re": "R",
    "i": "I", "int": "I", "inter": "I", "intermediate": "I", "sus inter": "I",
    "ii": "I",
    "s": "S", "sus": "S", "sens": "S", "sensitive": "S", "suscept": "S",
    "susceptible": "S", "susceptable": "S", "se": "S", "sa": "S",
    "nd": "", "not done": "", "nt": "", "na": "", "n a": "", "": "",
    "pending": "", "test": "", "u": "", "undetermined": "", "nr": "",
}

_POSITIVE_WORDS = {"positive", "pos", "detected", "detection", "present", "reactive",
                   "reagent", "yes", "identified", "growth", "presumptive positive",
                   "toxin positive", "amplified", "target detected", "true"}
_NEGATIVE_WORDS = {"negative", "neg", "not detected", "absent", "no", "non reactive",
                   "no growth", "not identified", "not isolated", "false", "none"}


def parse_interpretation(value) -> str:
    """Map any lab susceptibility wording to ``S`` / ``I`` / ``R`` (or ``''``)."""
    text = clean_cell(value)
    if not text:
        return ""
    n = normalise(text)
    if n in _RESULT_WORDS:
        return _RESULT_WORDS[n]
    # "R (>=32)", "32 R", "RESISTANT*", "S - Susceptible", "2+ R"
    m = re.search(r"\b(resistant|resistance|non susceptible|nonsusceptible)\b", n)
    if m:
        return "R"
    m = re.search(r"\b(intermediate|intermdiate)\b", n)
    if m:
        return "I"
    m = re.search(r"\b(susceptible|sensitive|susceptable|sus)\b", n)
    if m:
        return "S"
    for tok in reversed(n.split(" ")):
        if tok in _RESULT_WORDS and len(tok) <= 3:
            return _RESULT_WORDS[tok]
    if re.search(r"(^|[^a-z])(r|i|s)($|[^a-z])", n):
        # Last resort: a lone embedded letter code.
        letters = [t for t in n.split(" ") if t in {"r", "i", "s"}]
        if letters:
            return letters[-1].upper()
    return ""


def parse_flag(value) -> str:
    """Presence/absence wording -> 'positive' | 'negative' | ''."""
    n = normalise(value)
    if not n:
        return ""
    if n in _NEGATIVE_WORDS or any(phrase in n for phrase in
                                   ["not detected", "no growth", "not identified",
                                    "negative"]):
        return "negative"
    if n in _POSITIVE_WORDS or any(phrase in n for phrase in
                                   ["detected", "positive", "present", "identified",
                                    "growth seen", "reactive"]):
        return "positive"
    return ""


def mic_is_resistant(mic: str, result: str) -> bool:
    """MIC alone can carry the interpretation: '>=32' implies non-susceptible."""
    if result:
        return result == "R"
    m = re.search(r"(>=|=>|>|≥)\s*[\d.]+", clean_cell(mic))
    return bool(m)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@dataclass
class Sensitivity:
    antibiotic_raw: str = ""
    antibiotic: str = ""
    category: str = ""
    result: str = ""
    mic: str = ""
    zone: str = ""
    method: str = ""
    intrinsic: bool = False

    @property
    def non_susceptible(self) -> bool:
        return self.result in ("R", "I")


@dataclass
class LabRecord:
    """One normalised organism-isolate event for one patient."""
    patient_id: str = "UNKNOWN"
    patient_name: str = ""
    ward: str = "Unspecified"
    room: str = ""
    specimen_type: str = "Unspecified"
    sample_date: str = ""
    received_date: str = ""
    organism_raw: str = ""
    pathogen: str = ""
    confidence: float = 0.0
    match_method: str = "none"
    matched_phrase: str = ""
    other_label: str = ""
    markers: list[str] = field(default_factory=list)
    genus_only: bool = False
    suppressed: bool = False
    result_flag: str = "positive"
    notes: str = ""
    method: str = ""
    source_row: int = 0
    sensitivities: list[Sensitivity] = field(default_factory=list)
    _call: object | None = field(default=None, repr=False)

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_fields(cls, fields: dict[str, str], raw: dict, row_no: int = 0) -> "LabRecord":
        """Build a record from ``{semantic_field: raw_value}``."""
        get = lambda k: clean_cell(raw.get(fields.get(k, ""), "")) if fields.get(k) else ""
        rec = cls(source_row=row_no)
        rec.patient_id = normalise_patient(get("patient_id") or get("lab_no")
                                           or get("patient_name"))
        rec.patient_name = _mask_name(get("patient_name"))
        rec.ward = normalise_ward(get("ward"))
        rec.room = clean_cell(get("room"))[:24]
        rec.specimen_type = normalise_specimen(get("specimen_type"))
        rec.sample_date = (parse_date(get("sample_date"))
                           or parse_date(get("received_date"))
                           or parse_date(get("result_date")))
        rec.received_date = parse_date(get("received_date"))
        rec.notes = clean_cell(get("notes"))[:400]
        rec.method = clean_cell(get("method"))[:40]

        organism_text = " ".join(x for x in (get("organism"), get("result_flag"),
                                             get("result")) if x)
        call = identify_organism(get("organism") or organism_text)
        rec._call = call
        rec.organism_raw = clean_cell(get("organism"))[:120]
        rec.pathogen = call.pathogen or ""
        rec.confidence = call.confidence
        rec.match_method = call.method
        rec.matched_phrase = call.matched
        rec.other_label = call.other_label
        rec.markers = list(call.markers)
        rec.genus_only = call.genus_only
        rec.suppressed = call.suppressed

        flag = parse_flag(get("result_flag")) or parse_flag(get("result"))
        if flag:
            rec.result_flag = flag
        elif call.suppressed:
            rec.result_flag = "negative"
        if rec.result_flag == "negative":
            rec.suppressed = True
        return rec

    # ---- sensitivities ---------------------------------------------------
    def add_sensitivity(self, antibiotic_raw: str, result: str = "", mic: str = "",
                        zone: str = "", method: str = "") -> bool:
        ab = resolve_antibiotic(antibiotic_raw)
        interp = parse_interpretation(result)
        if not interp and mic_is_resistant(mic, ""):
            interp = "R"
        if not ab and not interp:
            return False
        if not ab:
            # Unrecognised agent name: keep it visible but unparsed.
            ab = ""
        sens = Sensitivity(
            antibiotic_raw=clean_cell(antibiotic_raw)[:80],
            antibiotic=ab or normalise(antibiotic_raw)[:40],
            category=category_of(ab) if ab else "other",
            result=interp, mic=clean_cell(mic)[:24], zone=clean_cell(zone)[:12],
            method=clean_cell(method)[:30] or self.method,
        )
        # De-duplicate: same agent, later non-empty result wins.
        for i, existing in enumerate(self.sensitivities):
            if existing.antibiotic == sens.antibiotic:
                if not existing.result or sens.result:
                    self.sensitivities[i] = sens
                return False
        self.sensitivities.append(sens)
        return True

    def add_profile_string(self, text: str) -> int:
        """Parse ``Amp:R; Gent:S; Cipro=R`` style compact panels."""
        added = 0
        for part in re.split(r"[;,|\n/]|(?:\s{2,})|(?:\s+and\s+)", clean_cell(text)):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^\s*(.+?)\s*[:=\-]\s*([SIRsir])\b", part)
            if m:
                added += bool(self.add_sensitivity(m.group(1), m.group(2)))
            else:
                m = re.match(r"^\s*(.+?)\s+(R|S|I|Resistant|Susceptible|Intermediate)\b",
                             part, re.I)
                if m:
                    added += bool(self.add_sensitivity(m.group(1), m.group(2)))
        return added

    def finalise(self) -> "LabRecord":
        """Apply antibiogram-driven reclassification (S. aureus -> MRSA)."""
        if self._call is not None:
            apply_phenotype_upgrades(self._call, self.sensitivities)
            self.pathogen = self._call.pathogen or ""
            self.confidence = self._call.confidence
            self.match_method = self._call.method
            if "MRSA" in self._call.markers and "MRSA" not in self.markers:
                self.markers.append("MRSA")
        return self

    # ---- predicates ------------------------------------------------------
    @property
    def is_target(self) -> bool:
        return bool(self.pathogen) and not self.suppressed

    @property
    def resistant_count(self) -> int:
        return sum(1 for s in self.sensitivities if s.result == "R")

    @property
    def tested_count(self) -> int:
        return sum(1 for s in self.sensitivities if s.result in ("S", "I", "R"))

    def key(self) -> tuple:
        return (self.patient_id, self.pathogen, self.specimen_type,
                self.sample_date, self.ward)


_NAME_RE = re.compile(r"^([A-Za-z]).+?([A-Za-z])$")


def _mask_name(name: str) -> str:
    """Keep an initial-only label; Bioguard never needs full patient names."""
    name = clean_cell(name)
    if not name:
        return ""
    parts = [p for p in re.split(r"[\s,]+", name) if p]
    if not parts:
        return ""
    initials = "".join(p[0].upper() for p in parts[:2])
    return initials + "•••"


def merge_records(records: list[LabRecord]) -> list[LabRecord]:
    """Collapse long-format rows describing the same isolate into one record."""
    out: dict[tuple, LabRecord] = {}
    order: list[tuple] = []
    for r in records:
        k = r.key()
        if k not in out:
            out[k] = r
            order.append(k)
            continue
        base = out[k]
        for s in r.sensitivities:
            base.add_sensitivity(s.antibiotic_raw or s.antibiotic, s.result,
                                 s.mic, s.zone, s.method)
        if not base.sample_date and r.sample_date:
            base.sample_date = r.sample_date
        if not base.room and r.room:
            base.room = r.room
        if not base.specimen_type and r.specimen_type:
            base.specimen_type = r.specimen_type
        if r.notes and r.notes not in base.notes:
            base.notes = (base.notes + " | " + r.notes)[:400]
        for m in r.markers:
            if m not in base.markers:
                base.markers.append(m)
    return [out[k] for k in order]
