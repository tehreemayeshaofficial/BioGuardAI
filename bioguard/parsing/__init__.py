"""
Upload dispatch: choose the right extractor for an incoming lab report.

The decision is made on the *magic bytes*, not the extension, because labs
routinely rename exports and browsers mangle file names.
"""

from __future__ import annotations

from pathlib import Path

from ..textutil import clean_cell
from .common import LabRecord, Sensitivity, merge_records
from .csv_parser import ParseOutcome, parse_csv_bytes, parse_csv_text
from .pdf_parser import parse_pdf_bytes

__all__ = ["ParseOutcome", "LabRecord", "Sensitivity", "merge_records",
           "parse_upload", "parse_csv_bytes", "parse_csv_text", "parse_pdf_bytes"]


def parse_upload(filename: str, data: bytes) -> ParseOutcome:
    """Parse one uploaded file, returning every record found plus diagnostics."""
    name = clean_cell(filename) or "upload"
    if not data:
        out = ParseOutcome()
        out.errors.append("The file is empty.")
        return out

    if data[:5] == b"%PDF-" or data[:4] == b"%PDF":
        return parse_pdf_bytes(data, name)

    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        # Extension says PDF, bytes disagree - try text anyway.
        out = parse_pdf_bytes(data, name)
        if out.records:
            return out
    return parse_csv_bytes(data, name)
