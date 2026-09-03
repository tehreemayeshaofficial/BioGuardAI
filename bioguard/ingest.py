"""
Ingestion service: the only code allowed to write surveillance data.

Routes stay thin because everything messy about lab intake lives here - content
sniffing, duplicate detection, the audit row, and the transaction that turns a
parsed report into isolates plus antibiograms.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from . import database
from .pathogens import display_name
from .parsing import parse_upload

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class IngestResult:
    """Outcome of one file, suitable for both flash messages and JSON."""
    report_id: int = 0
    filename: str = ""
    status: str = "error"          # imported | duplicate | empty | error
    isolates: int = 0
    sensitivities: int = 0
    target_hits: int = 0
    patients: int = 0
    rows_read: int = 0
    rows_skipped: int = 0
    layout: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate_of: int = 0
    unmapped: list[str] = field(default_factory=list)
    # Per-pathogen counts so the UI can say what was actually found.
    found: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("imported", "duplicate") and self.isolates > 0

    def as_dict(self) -> dict:
        return {
            "report_id": self.report_id, "filename": self.filename,
            "status": self.status, "isolates": self.isolates,
            "sensitivities": self.sensitivities, "target_hits": self.target_hits,
            "patients": self.patients, "rows_read": self.rows_read,
            "rows_skipped": self.rows_skipped, "layout": self.layout,
            "message": self.message, "errors": self.errors,
            "warnings": self.warnings, "duplicate_of": self.duplicate_of,
            "unmapped": self.unmapped,
            "found": [{"pathogen": k, "name": display_name(k), "count": v}
                      for k, v in sorted(self.found.items(), key=lambda x: -x[1])],
        }


def safe_filename(name: str) -> str:
    """Strip anything a browser or a careless lab tech may have left behind."""
    base = Path((name or "upload").replace("\\", "/")).name
    base = _UNSAFE.sub("_", base).strip("._") or "upload"
    return base[-120:]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_file(data: bytes, digest: str, filename: str, upload_dir: str) -> str:
    """Persist the original bytes for audit; return the stored name."""
    out_dir = Path(upload_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stored = f"{stamp}-{digest[:12]}-{safe_filename(filename)}"
    (out_dir / stored).write_bytes(data)
    return stored


def ingest_bytes(conn, filename: str, data: bytes, *,
                 upload_dir: str = config.UPLOAD_DIR, source: str = "upload",
                 keep_file: bool = config.KEEP_UPLOADED_FILES,
                 allow_duplicate: bool = False) -> IngestResult:
    """Parse and persist one lab report. Never raises for bad input."""
    name = safe_filename(filename)
    result = IngestResult(filename=name)
    digest = sha256(data)

    existing = conn.execute(
        "SELECT id, filename, isolates FROM reports WHERE sha256 = ? AND source = ?",
        (digest, "upload")).fetchone()
    if existing and not allow_duplicate:
        result.status = "duplicate"
        result.duplicate_of = int(existing["id"])
        result.report_id = result.duplicate_of
        result.message = (f"Identical to report #{result.duplicate_of} "
                          f"({existing['filename']}) - nothing new was imported.")
        return result

    stored = _store_file(data, digest, name, upload_dir) if keep_file else ""

    try:
        outcome = parse_upload(name, data)
    except Exception as exc:                      # noqa: BLE001 - a parse crash
        result.status = "error"                   # must not take the request down
        result.message = f"The file could not be read: {exc.__class__.__name__}."
        result.errors.append(str(exc)[:300])
        return result

    records = outcome.records
    report_id = database.create_report(
        conn, filename=name,
        file_type=(Path(name).suffix.lower().lstrip(".") or "txt"),
        file_size=len(data), sha256=digest, source=source, stored_name=stored,
        layout=outcome.layout, status="parsing",
        rows_read=outcome.rows_read, rows_skipped=outcome.rows_skipped,
        parse_log=_log_text(outcome))
    result.report_id = report_id
    result.layout = outcome.layout
    result.rows_read = outcome.rows_read
    result.rows_skipped = outcome.rows_skipped
    result.errors = list(outcome.errors)
    result.warnings = list(outcome.warnings)
    result.unmapped = list(outcome.unmapped_columns)

    if not records:
        result.status = "empty"
        result.message = ("No organism results could be read from this file. "
                          "Check that it has patient, organism and date columns.")
        database.update_report(conn, report_id, status="empty", isolates=0,
                               sensitivities=0, message=result.message)
        return result

    n_iso, n_sen = database.insert_records(conn, report_id, records)
    targets = [r for r in records if r.pathogen and not r.suppressed]
    result.isolates = n_iso
    result.sensitivities = n_sen
    result.target_hits = sum(1 for r in targets)
    result.patients = len({r.patient_id for r in targets})
    for r in targets:
        result.found[r.pathogen] = result.found.get(r.pathogen, 0) + 1

    if n_iso == 0:
        # Every row collided with an isolate already stored - re-upload of a
        # partially-known report.
        result.status = "duplicate"
        result.message = ("All results in this file were already in the "
                          "database; nothing was added.")
    else:
        result.status = "imported"
        result.message = (_plural(n_iso, "isolate") + ", "
                          + _plural(n_sen, "sensitivity") + " stored")
    database.update_report(conn, report_id, status=result.status,
                           isolates=n_iso, sensitivities=n_sen,
                           message=result.message, parse_log=_log_text(outcome))
    return result


def ingest_upload(conn, file_storage, *, upload_dir: str = config.UPLOAD_DIR,
                  allow_duplicate: bool = False) -> IngestResult:
    """Handle a Werkzeug FileStorage from a multipart form."""
    name = safe_filename(file_storage.filename or "")
    suffix = Path(name).suffix.lower()
    if suffix not in config.ALLOWED_EXTENSIONS:
        out = IngestResult(filename=name, status="error")
        allowed = ", ".join(sorted(e.lstrip(".").upper() for e in config.ALLOWED_EXTENSIONS))
        out.message = f"Unsupported file type '{suffix or 'unknown'}'. Allowed: {allowed}."
        return out
    return ingest_bytes(conn, name, file_storage.read(), upload_dir=upload_dir,
                        allow_duplicate=allow_duplicate)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _log_text(outcome) -> str:
    """A compact audit trail a lab manager can actually read."""
    lines = []
    if outcome.layout:
        lines.append(f"Detected layout: {outcome.layout}")
    if outcome.headers:
        lines.append("Header row: " + " | ".join(outcome.headers[:40]))
    if outcome.columns_found:
        lines.append("Column mapping: " + ", ".join(
            f"{k}<-{v}" for k, v in sorted(outcome.columns_found.items())))
    if outcome.panel_columns:
        lines.append(f"Antibiogram columns recognised: {outcome.panel_columns}")
    for w in outcome.warnings:
        lines.append(f"WARNING: {w}")
    for e in outcome.errors:
        lines.append(f"ERROR: {e}")
    return "\n".join(lines)[:20000]


# --------------------------------------------------------------------------
# Demo dataset
# --------------------------------------------------------------------------
def demo_is_seeded(conn) -> bool:
    return database.get_meta(conn, "demo_seeded") == "1"


def seed_demo(conn, *, force: bool = False) -> dict:
    """Load the synthetic hospital dataset when the database is empty."""
    from .demo_data import build_records

    if demo_is_seeded(conn) and not force:
        return {"status": "already", "isolates": 0, "message": "Demo data already loaded."}
    if database.count_reports(conn) and not (force and demo_is_seeded(conn)):
        return {"status": "skipped", "isolates": 0,
                "message": "Real reports are present - demo data not added."}

    records = build_records()
    report_id = database.create_report(
        conn, filename="demo-hospital-dataset.csv", file_type="csv",
        file_size=0, sha256="demo", source="demo", stored_name="",
        layout="demo", status="imported", rows_read=len(records),
        message="Synthetic surveillance dataset demonstrating every detection scenario.")
    n_iso, n_sen = database.insert_records(conn, report_id, records)
    database.update_report(conn, report_id, isolates=n_iso, sensitivities=n_sen)
    database.set_meta(conn, "demo_seeded", "1")
    return {"status": "seeded", "isolates": n_iso, "sensitivities": n_sen,
            "message": f"Demo dataset loaded: {_plural(n_iso, 'isolate')}, "
                       f"{_plural(n_sen, 'sensitivity')}."}


def wipe_demo(conn) -> int:
    """Remove demo rows only; uploaded reports are untouched."""
    cur = conn.execute("DELETE FROM sensitivities WHERE isolate_id IN "
                       "(SELECT id FROM isolates WHERE report_id IN "
                       "(SELECT id FROM reports WHERE source='demo'))")
    del_iso = cur.rowcount
    conn.execute("DELETE FROM isolates WHERE report_id IN "
                 "(SELECT id FROM reports WHERE source='demo')")
    conn.execute("DELETE FROM reports WHERE source='demo'")
    database.set_meta(conn, "demo_seeded", "")
    conn.commit()
    return del_iso


def purge_uploads(conn, upload_dir: str = config.UPLOAD_DIR) -> int:
    """Delete stored originals for reports that no longer exist."""
    live = {r["stored_name"] for r in conn.execute(
        "SELECT stored_name FROM reports WHERE stored_name IS NOT NULL")}
    removed = 0
    out_dir = Path(upload_dir)
    if not out_dir.exists():
        return 0
    for p in out_dir.iterdir():
        if p.name not in live:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def uploaded_file_path(report, upload_dir: str = config.UPLOAD_DIR) -> Path | None:
    stored = (report["stored_name"] or "") if report else ""
    if not stored:
        return None
    path = Path(upload_dir) / stored
    return path if path.exists() else None


def reset_all(conn, upload_dir: str = config.UPLOAD_DIR) -> None:
    database.reset_database(conn)
    out_dir = Path(upload_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
