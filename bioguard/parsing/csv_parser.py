"""
CSV / TSV laboratory-report importer.

Three real-world export shapes are supported and auto-detected per file:

1. **Long panel** - one row per organism/antibiotic pair::

       patient_id, ward, date, specimen, organism, antibiotic, result, mic

2. **Wide panel** - one row per isolate, with an antibiotic per column::

       patient_id, ward, date, specimen, organism, Ampicillin, Meropenem, ...

3. **Detection only** - one row per positive (or negative) screen::

       patient_id, ward, date, organism, detected

Shape 3 is what rapid-PCR and C. difficile toxin screens produce, and it still
counts towards transmission trends and outbreak risk.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from ..textutil import clean_cell, normalise
from ..antibiotics import resolve_antibiotic
from .common import LabRecord, map_columns, parse_flag, parse_interpretation

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1", "utf-16", "mac-roman")

# Rows whose first cell is one of these are report furniture, not data.
_JUNK_PREFIXES = (
    "page ", "www.", "http", "confidential", "printed", "lab ", "report #",
    "generated", "note:", "note :", "*r", "s =", "i =", "r =",
)


@dataclass
class ParseOutcome:
    """Everything a caller needs to know about one uploaded file."""
    records: list[LabRecord] = field(default_factory=list)
    rows_read: int = 0
    rows_skipped: int = 0
    headers: list[str] = field(default_factory=list)
    layout: str = ""              # 'long' | 'wide' | 'detection' | 'unknown'
    columns_found: dict[str, str] = field(default_factory=dict)
    panel_columns: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.records)


# --------------------------------------------------------------------------
# File-level helpers
# --------------------------------------------------------------------------
def decode_bytes(data: bytes) -> tuple[str, str]:
    """Return (text, encoding) trying the encodings labs actually produce."""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (lossy)"


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    first = (sample.splitlines() or [""])[0]
    counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] >= 2 else ","


def _looks_junk(row: list[str]) -> bool:
    joined = " ".join(clean_cell(c) for c in row).strip()
    if not joined:
        return True
    low = joined.lower()
    return any(low.startswith(p) for p in _JUNK_PREFIXES)


def _row_is_header(row: list[str]) -> bool:
    fields, antibiotics = map_columns(row)
    return len(fields) >= 2 or (len(antibiotics) >= 3 and "organism" in fields)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def parse_csv_bytes(data: bytes, filename: str = "") -> ParseOutcome:
    text, encoding = decode_bytes(data)
    out = parse_csv_text(text, filename)
    out.warnings.append(f"Decoded as {encoding}")
    return out


def parse_csv_text(text: str, filename: str = "") -> ParseOutcome:
    outcome = ParseOutcome(raw_text=text)
    rows = _read_rows(text)
    if not rows:
        outcome.errors.append("The file contains no readable rows.")
        return outcome

    header_idx, fields, panel = _locate_header(rows)
    if header_idx is None:
        outcome.errors.append(
            "No recognisable header row. Expected columns such as "
            "'patient_id', 'ward', 'date', 'organism' and 'antibiotic'/'result'.")
        outcome.rows_read = len(rows)
        return outcome

    header = [clean_cell(h) for h in rows[header_idx]]
    outcome.headers = header
    outcome.columns_found = fields
    outcome.panel_columns = len(panel)
    outcome.rows_read = len(rows) - header_idx - 1

    mapped_headers = set(fields.values()) | set(panel.values())
    outcome.unmapped_columns = [h for h in header
                                if h and h not in mapped_headers]

    body = rows[header_idx + 1:]
    layout = _detect_layout(fields, panel, body)
    outcome.layout = layout

    records: list[LabRecord] = []
    context: dict[str, str] = {}      # carry-forward for blank repeated cells

    for offset, row in enumerate(body):
        row_no = header_idx + offset + 2
        cells = [clean_cell(c) for c in row]
        if not any(cells):
            continue
        if _looks_junk(cells) and not _row_has_data(cells):
            outcome.rows_skipped += 1
            continue

        raw = {header[i]: (cells[i] if i < len(cells) else "")
               for i in range(len(header))}

        # Blank repeated ward/patient cells inherit the previous non-blank value,
        # exactly as they appear in merged-cell LIS exports.
        before = {fld: raw.get(col, "") for fld, col in fields.items() if col}
        for fld in ("patient_id", "patient_name", "ward", "specimen_type",
                    "sample_date", "organism", "lab_no"):
            col = fields.get(fld)
            if not col:
                continue
            val = raw.get(col, "")
            if val:
                context[fld] = val
            elif context.get(fld) and layout != "long":
                raw[col] = context[fld]

        # A row that names its own patient but reports no organism is a no-growth
        # line, not a continuation of the block above: an organism inherited here
        # would fabricate an isolate nobody sent. Only a row whose patient cell is
        # *also* blank is the second half of a merged cell.
        org_col = fields.get("organism")
        if org_col and not before.get("organism") and before.get("patient_id"):
            raw[org_col] = ""

        rec = _build_record(raw, header, fields, panel, layout, row_no, outcome)
        if rec is None:
            outcome.rows_skipped += 1
            continue
        records.append(rec)

    from .common import merge_records
    merged = merge_records(records)
    for r in merged:
        r.finalise()
    outcome.records = merged
    if not merged:
        outcome.errors.append(
            "The header was understood but no rows produced a usable organism. "
            "Check that the 'organism' column points at the right header.")
    return outcome


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------
def _read_rows(text: str) -> list[list[str]]:
    delimiter = sniff_delimiter(text[:8000])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        if row:
            rows.append(row)
    return rows


def _locate_header(rows: list[list[str]]) -> tuple[int | None, dict, dict]:
    """Scan the first 40 rows for the best header candidate."""
    best: tuple[int | None, dict, dict, int] = (None, {}, {}, -1)
    for i, row in enumerate(rows[:40]):
        if not row or _looks_junk(row):
            continue
        fields, panel = map_columns(row)
        score = 0
        for required in ("patient_id", "organism", "ward", "sample_date"):
            if required in fields:
                score += 3
        if "antibiotic" in fields and "result" in fields:
            score += 4
        score += len(panel)
        score += len(fields) * 0.5
        if score > best[3]:
            best = (i, fields, panel, score)
    return best[0], best[1], best[2]


def _detect_layout(fields: dict, panel: dict, body: list[list[str]]) -> str:
    if "antibiotic" in fields and "result" in fields:
        return "long"
    if panel:
        return "wide"
    if "resistance_profile" in fields:
        return "profile"
    return "detection"


def _row_has_data(cells: list[str]) -> bool:
    """Decide whether a row is an observation rather than a line of furniture.

    Report banners ("Page 1 of 3", "Note: ...", "www.lab.invalid") occupy the
    first cell and leave the rest of the row empty, while a real isolate row
    populates several columns. Treating any non-empty row as data lets those
    banners fall through to the carry-forward pass below and come back as
    phantom isolates attributed to a patient called "NOTE:...".
    """
    return any(clean_cell(c) for c in cells[1:])


def _build_record(raw: dict, header: list[str], fields: dict, panel: dict,
                  layout: str, row_no: int, outcome: ParseOutcome) -> LabRecord | None:
    rec = LabRecord.from_fields(fields, raw, row_no)

    if layout == "long":
        ab_raw = raw.get(fields.get("antibiotic", ""), "")
        res = raw.get(fields.get("result", ""), "")
        mic = raw.get(fields.get("mic", ""), "")
        zone = raw.get(fields.get("zone", ""), "")
        # A long-format row with no organism repeats the header context: the
        # isolate is still real, so keep the sensitivity even if unnamed.
        rec.add_sensitivity(ab_raw, res, mic, zone,
                            raw.get(fields.get("method", ""), ""))

    if layout in ("wide", "profile", "detection", "long") and panel:
        for ab_key, col in panel.items():
            value = raw.get(col, "")
            if not clean_cell(value):
                continue
            rec.add_sensitivity(col, value)

    profile_col = fields.get("resistance_profile")
    if profile_col:
        text = raw.get(profile_col, "")
        if text:
            rec.add_profile_string(text)

    # Sensitivity-free organism-only rows still matter (screening data).
    flag_val = raw.get(fields.get("result_flag", ""), "") if fields.get("result_flag") else ""
    if flag_val:
        f = parse_flag(flag_val)
        if f:
            rec.result_flag = f
            if f == "negative":
                rec.suppressed = True

    if not rec.organism_raw and not rec.sensitivities:
        return None
    if not rec.pathogen and rec.match_method in ("none", "nontarget"):
        # Track non-target organisms only when they carry a real name.
        if not rec.organism_raw:
            return None
    return rec
