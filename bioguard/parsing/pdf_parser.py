"""
PDF laboratory-report importer.

PDF is the format most hospital microbiology laboratories actually issue, and
it is unstructured by nature. Bioguard runs two complementary extractors over
every page and merges their output:

* a **table extractor** (pdfplumber ruled-table detection) for LIS exports whose
  antibiogram is a real table;
* a **text extractor** that understands the conventional free-text layout -
  ``Patient ID:``/``Ward:``/``Collected:`` header block, an
  ``Organism:`` line, then one antibiotic per line with its S/I/R call and MIC.

Values carry forward within a report, so an antibiogram whose patient block
appears only once at the top of page 1 is still attributed correctly.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from ..antibiotics import ANTIBIOTICS, resolve_antibiotic
from ..detection import identify_organism
from ..pathogens import PATHOGENS
from ..textutil import clean_cell, collapse_ws, normalise, phrase_in
from .common import (COLUMN_ALIASES, LabRecord, map_columns, parse_flag,
                     parse_interpretation)

try:                                     # pragma: no cover - environment guard
    import pdfplumber
except ImportError:                       # pragma: no cover
    pdfplumber = None                     # type: ignore


# --------------------------------------------------------------------------
# Vocabulary assembled from the antibiotic alias index
# --------------------------------------------------------------------------
_RESULT_TOKEN = (r"(?:resistant|resistence|non[\s\-]?susceptible|susceptible|"
                 r"sensitive|intermediate|res|sus|sens|inter|nd|r|s|i)")

_INTERP_ONLY = {"r", "s", "i", "res", "sus", "sens", "inter", "nd"}


def _alias_alternatives() -> str:
    """Regex alternation of every antibiotic alias, longest first."""
    words: set[str] = set()
    for ab in ANTIBIOTICS.values():
        words.add(ab.name)
        words.update(ab.aliases)
    cleaned = sorted({normalise(w) for w in words if len(normalise(w)) >= 3},
                     key=len, reverse=True)
    return "|".join(re.escape(w).replace(r"\ ", r"[\s\-/]+") for w in cleaned)


_AB_ALIAS_RE: re.Pattern | None = None


def _ab_re() -> re.Pattern:
    global _AB_ALIAS_RE
    if _AB_ALIAS_RE is None:
        _AB_ALIAS_RE = re.compile(
            r"(?<![a-z])(" + _alias_alternatives() + r")(?![a-z])", re.I)
    return _AB_ALIAS_RE


_FIELD_KEYS: dict[str, str] | None = None


def _field_keys() -> dict[str, str]:
    global _FIELD_KEYS
    if _FIELD_KEYS is None:
        idx: dict[str, str] = {}
        for fld, aliases in COLUMN_ALIASES.items():
            for a in aliases:
                idx.setdefault(normalise(a), fld)
            idx.setdefault(normalise(fld), fld)
        _FIELD_KEYS = dict(sorted(idx.items(), key=lambda kv: -len(kv[0])))
    return _FIELD_KEYS


_KV_RE = re.compile(r"^\s*(?:\d{1,2}[\).\-\s])?([a-z][a-z0-9 /#()\-.]{0,44}?)"
                    r"\s*[:\-–—]\s*(.*)$", re.I)
_INLINE_DATE_RE = re.compile(r"(\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4})")
_ID_INLINE_RE = re.compile(r"\b(?:mrn|hospital\s*no|patient\s*id|accession)\s*[:#]?\s*"
                           r"([A-Za-z0-9\-/]{3,20})", re.I)
_MIC_RE = re.compile(r"^\s*[:=]?\s*((?:>=|<=|>|<|≥|≤)?\s*\d+(?:[.,]\d+)?)", re.I)
# NOTE: no ``^`` anchor on the two patterns below. They are only ever used as
# ``.match(text, pos)`` to test a cursor part-way through a line, and ``^``
# matches the start of the *string*, never ``pos`` - so anchoring them silently
# made every leader and MIC fail to match after the first character.
_LEADERS = re.compile(r"[\s.=\u2013\u2014\-:()#*,]{0,18}")


# --------------------------------------------------------------------------
# Accumulator
# --------------------------------------------------------------------------
@dataclass
class _Context:
    patient_id: str = ""
    patient_name: str = ""
    ward: str = ""
    room: str = ""
    specimen_type: str = ""
    sample_date: str = ""
    lab_no: str = ""
    organism: str = ""
    notes: str = ""
    result_flag: str = ""
    method: str = ""
    pending_key: str = ""

    def snapshot(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "pending_key" and v}


class _Accumulator:
    """Holds the report currently being read and emits LabRecords on flush."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.ctx = _Context()
        self.sens: list[tuple[str, str, str]] = []
        self.records: list[LabRecord] = []
        self.row_no = 0

    # -- context ----------------------------------------------------------
    def set(self, fld: str, value: str) -> bool:
        value = collapse_ws(value)
        if not value:
            return False
        if fld == "organism":
            if self.ctx.organism and self.ctx.organism != value:
                self.flush()
            self.ctx.organism = value
            return True
        if fld == "patient_id":
            if self.ctx.patient_id and self.ctx.patient_id != value:
                self.flush()
            self.ctx.patient_id = value
            return True
        if fld == "result_flag":
            flag = parse_flag(value)
            if flag:
                self.ctx.result_flag = flag
            return True
        if fld in ("notes", "method", "room", "specimen_type", "sample_date",
                   "ward", "patient_name", "received_date", "result_date",
                   "clinician", "age", "sex", "date_of_birth", "lab_no"):
            setattr(self.ctx, fld, value)
            return True
        return False

    def add_sensitivity(self, ab_raw: str, result: str, mic: str = "") -> None:
        self.sens.append((ab_raw, result, mic))

    def flush(self) -> None:
        if not (self.ctx.organism or self.sens):
            self.sens = []
            return
        raw = self.ctx.snapshot()
        fields = {k: k for k in raw}
        rec = LabRecord.from_fields(fields, raw, self.row_no)
        for ab_raw, result, mic in self.sens:
            rec.add_sensitivity(ab_raw, result, mic)
        rec.finalise()
        if rec.organism_raw or rec.sensitivities:
            self.records.append(rec)
        self.sens = []
        self.ctx.organism = ""
        self.ctx.notes = ""
        self.ctx.result_flag = ""

    def finish(self) -> list[LabRecord]:
        self.flush()
        return self.records


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def parse_pdf_bytes(data: bytes, filename: str = ""):
    from .csv_parser import ParseOutcome
    outcome = ParseOutcome()
    if pdfplumber is None:
        outcome.errors.append("pdfplumber is not installed - PDF import unavailable.")
        return outcome

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_texts: list[str] = []
            page_tables: list[list[list[list[str]]]] = []
            for page in pdf.pages[:60]:
                try:
                    page_texts.append(page.extract_text() or "")
                except Exception:                      # pragma: no cover
                    page_texts.append("")
                try:
                    page_tables.append(page.extract_tables() or [])
                except Exception:                      # pragma: no cover
                    page_tables.append([])
    except Exception as exc:                           # pragma: no cover
        outcome.errors.append(f"Could not read the PDF: {exc}")
        return outcome

    text = "\n".join(page_texts)
    outcome.raw_text = text
    if not text.strip() and not any(page_tables):
        outcome.errors.append(
            "No selectable text found - this PDF is probably a scanned image. "
            "Run OCR first, or upload the CSV export instead.")
        return outcome

    acc = _Accumulator(outcome)
    for page_no, page_text in enumerate(page_texts, start=1):
        _parse_text_page(page_text, acc, page_no, outcome)

    text_records = acc.finish()

    tbl_acc = _Accumulator(outcome)
    for page_no, tables in enumerate(page_tables, start=1):
        for table in tables:
            _parse_table(table, tbl_acc, outcome)
    table_records = tbl_acc.records

    from .common import merge_records
    merged = merge_records(text_records + table_records)
    for r in merged:
        r.finalise()
    outcome.records = [r for r in merged if r.organism_raw or r.sensitivities]
    outcome.rows_read = sum(len(p.splitlines()) for p in page_texts) + len(table_records)
    outcome.layout = "pdf"
    if not outcome.records:
        outcome.errors.append(
            "No organisms could be identified in this PDF. If it is a scanned "
            "image, upload the CSV export instead.")
    return outcome


# --------------------------------------------------------------------------
# Text strategy
# --------------------------------------------------------------------------
def _parse_text_page(text: str, acc: _Accumulator, page_no: int,
                     outcome) -> None:
    lines = [collapse_ws(l) for l in text.splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        acc.row_no = i
        if _consume_line(line, lines, acc, outcome, page_no):
            continue
    # page break inside one report: keep the context, do not flush


def _consume_line(line: str, lines: list[str], acc: _Accumulator,
                  outcome, page_no: int) -> bool:
    low = line.lower().strip()

    # --- section banners -------------------------------------------------
    if re.fullmatch(r"(sensitivity|susceptibility|antibiogram|sensitivities|"
                    r"susceptibility testing|culture and sensitivity|"
                    r"antimicrobial susceptibility)[ :]*", low):
        return True
    if re.fullmatch(r"[\s\-–—.=_*#]{2,}", low):
        return True

    # --- pending key from a wrapped label ("Organism identified:") -------
    if acc.ctx.pending_key:
        fld, acc.ctx.pending_key = acc.ctx.pending_key, ""
        m = _KV_RE.match(line) if line else None
        key = normalise(m.group(1)) if m else ""
        # Treat the line as a *new* field only when its leading word is itself a
        # known label or agent name. _KV_RE also fires on hyphens, so a wrapped
        # value such as "S-6001" would otherwise be thrown away as though it read
        # "S: 6001", and the isolate would be filed under the previous patient.
        starts_field = bool(m) and len(key) >= 3 and (
            key in _field_keys() or resolve_antibiotic(key))
        if starts_field:
            acc.ctx.pending_key = fld
        elif line:
            acc.set(fld, line)
            return True

    # --- explicit "KEY: VALUE" -------------------------------------------
    m = _KV_RE.match(line)
    if m:
        key, value = normalise(m.group(1)), clean_cell(m.group(2))
        fld = _field_keys().get(key)
        if fld is None:
            for alias, mapped in _field_keys().items():
                if len(alias) >= 5 and phrase_in(alias, key):
                    fld = mapped
                    break
        if fld:
            if value:
                acc.set(fld, value)
                if fld == "organism":
                    _harvest_sensitivities(value, acc)
            else:
                acc.ctx.pending_key = fld
            return True
        ab = resolve_antibiotic(key)
        if ab and value:
            interp = parse_interpretation(value)
            if interp:
                acc.add_sensitivity(m.group(1), interp, _mic_after(value[len(interp):]))
                return True

    # --- inline identifiers ("Mrn 40122 ward icu") -----------------------
    idm = _ID_INLINE_RE.search(line)
    if idm and not acc.ctx.patient_id:
        acc.ctx.patient_id = idm.group(1)

    # --- antibiotic / result pairs ---------------------------------------
    pairs = _harvest_sensitivities(line, acc)
    if pairs:
        return True

    # --- bare organism line ----------------------------------------------
    n = normalise(line)
    if len(n) >= 4 and not re.search(r"\d{3,}", n):
        call = identify_organism(line)
        if call.pathogen and call.confidence >= 0.65:
            acc.set("organism", line)
            return True
        if call.method == "nontarget" and len(line) < 60:
            # Record the name so the UI can show what was ignored.
            acc.set("organism", line)
            return True
    return False


def _mic_after(text: str) -> str:
    m = _MIC_RE.match(text or "")
    return clean_cell(m.group(1)) if m else ""


_VALUE_RE = re.compile(r"\s*(\d+(?:[.,]\d+)?)")


def _harvest_sensitivities(line: str, acc: _Accumulator) -> int:
    """Find every ``<antibiotic><leader><S|I|R>[<MIC>]`` pair on one text line.

    Works on the *normalised* line, so dot-leaders and column spacing are
    already flattened. Returns the number of pairs captured.
    """
    text = normalise(line)
    if not text:
        return 0
    count = 0
    pos = 0
    rx = _ab_re()
    while pos < len(text):
        m = rx.search(text, pos)
        if not m:
            break
        cursor = m.end()
        lead = _LEADERS.match(text, cursor)
        if lead:
            cursor = lead.end()
        im = re.match(_RESULT_TOKEN, text[cursor:], re.I)
        interp = parse_interpretation(im.group(0)) if im else ""
        if not interp:
            pos = m.end() + 1
            continue
        cursor += len(im.group(0))
        tail_lead = _LEADERS.match(text, cursor)
        if tail_lead:
            cursor = tail_lead.end()
        mic = ""
        mm = _VALUE_RE.match(text, cursor)
        if mm:
            mic = mm.group(1)
            cursor = mm.end()
        acc.add_sensitivity(m.group(1), interp, mic)
        count += 1
        pos = max(cursor, m.end()) + 1
    return count


# --------------------------------------------------------------------------
# Table strategy
# --------------------------------------------------------------------------
def _parse_table(table: list, acc: _Accumulator, outcome) -> None:
    rows = [[clean_cell(c) for c in (r or [])] for r in table]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return

    header_idx, fields, panel = None, {}, {}
    for i, row in enumerate(rows[:4]):
        f, p = map_columns(row)
        if len(f) >= 2 or len(p) >= 2:
            header_idx, fields, panel = i, f, p
            break

    if header_idx is not None and (panel or ("antibiotic" in fields and "result" in fields)):
        header = rows[header_idx]
        for row in rows[header_idx + 1:]:
            raw = {header[c]: (row[c] if c < len(row) else "")
                   for c in range(len(header))}
            rec = LabRecord.from_fields(fields, raw, 0)
            long_form = "antibiotic" in fields and "result" in fields
            if long_form:
                rec.add_sensitivity(raw.get(fields["antibiotic"], ""),
                                    raw.get(fields["result"], ""))
            for col in panel.values():
                value = raw.get(col, "")
                if value:
                    rec.add_sensitivity(col, value)
            if not rec.organism_raw and not rec.sensitivities:
                continue
            acc.finalise_row(rec)
        return

    # Three-column antibiogram table: Agent | Result | MIC
    if all(len(r) >= 2 for r in rows):
        for row in rows:
            first = row[0]
            if not first or map_columns([first])[0]:
                continue
            ab = resolve_antibiotic(first)
            if not ab:
                continue
            result = next((parse_interpretation(c) for c in row[1:] if parse_interpretation(c)), "")
            mic = next((c for c in row[1:] if re.search(r"\d", c) and not parse_interpretation(c)), "")
            if result or mic:
                acc.add_sensitivity(first, result, mic)


def _finalise_row(self, rec: LabRecord) -> None:      # bound below
    rec.finalise()
    self.records.append(rec)


_Accumulator.finalise_row = _finalise_row              # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Diagnostics used by the upload preview
# --------------------------------------------------------------------------
def describe_targets() -> list[str]:
    return [PATHOGENS[k].short_name for k in PATHOGENS]
