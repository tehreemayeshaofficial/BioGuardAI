"""
SQLite persistence for Bioguard AI.

Two tables carry the surveillance record - ``isolates`` (one row per
patient/organism event) and ``sensitivities`` (one row per agent tested) - plus
``reports`` to keep every uploaded file auditable and individually deletable.

The analysis engines do not issue SQL: they call :func:`load_dataset`, which
returns typed :class:`Iso` objects. Keeping SQL in one file makes the analytics
code trivially testable and avoids N+1 queries on the dashboard.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .antibiotics import category_of
from .pathogens import PATHOGENS

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    stored_name     TEXT,
    file_type       TEXT,
    file_size       INTEGER DEFAULT 0,
    sha256          TEXT,
    source          TEXT DEFAULT 'upload',
    uploaded_at     TEXT,
    rows_read       INTEGER DEFAULT 0,
    rows_skipped    INTEGER DEFAULT 0,
    isolates        INTEGER DEFAULT 0,
    sensitivities   INTEGER DEFAULT 0,
    layout          TEXT,
    status          TEXT,
    message         TEXT,
    parse_log       TEXT
);

CREATE TABLE IF NOT EXISTS isolates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER REFERENCES reports(id) ON DELETE CASCADE,
    patient_id      TEXT NOT NULL,
    patient_name    TEXT,
    ward            TEXT,
    room            TEXT,
    specimen_type   TEXT,
    sample_date     TEXT,
    sample_month    TEXT,
    organism_raw    TEXT,
    pathogen        TEXT,
    confidence      REAL DEFAULT 0,
    match_method    TEXT,
    matched_phrase  TEXT,
    other_label     TEXT,
    markers         TEXT DEFAULT '[]',
    genus_only      INTEGER DEFAULT 0,
    suppressed      INTEGER DEFAULT 0,
    result_flag     TEXT,
    notes           TEXT,
    source_row      INTEGER,
    UNIQUE (report_id, patient_id, pathogen, specimen_type, sample_date)
);

CREATE TABLE IF NOT EXISTS sensitivities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isolate_id      INTEGER REFERENCES isolates(id) ON DELETE CASCADE,
    antibiotic_raw  TEXT,
    antibiotic      TEXT,
    category        TEXT,
    result          TEXT,
    mic             TEXT,
    intrinsic       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY, v TEXT
);

CREATE INDEX IF NOT EXISTS ix_iso_pathogen   ON isolates(pathogen);
CREATE INDEX IF NOT EXISTS ix_iso_date       ON isolates(sample_date);
CREATE INDEX IF NOT EXISTS ix_iso_ward       ON isolates(ward);
CREATE INDEX IF NOT EXISTS ix_iso_patient    ON isolates(patient_id);
CREATE INDEX IF NOT EXISTS ix_iso_report     ON isolates(report_id);
CREATE INDEX IF NOT EXISTS ix_sen_iso        ON sensitivities(isolate_id);
CREATE INDEX IF NOT EXISTS ix_sen_ab         ON sensitivities(antibiotic);
CREATE INDEX IF NOT EXISTS ix_sen_result     ON sensitivities(result);
"""


# The registry database: who exists and which workspace they own. It is kept
# physically apart from every hospital's surveillance data, so tenancy is
# decided once, centrally, and no hospital's rows ever live here.
SCHEMA_ACCOUNTS = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS hospitals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    created_at      TEXT,
    status          TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    hospital_id     INTEGER NOT NULL REFERENCES hospitals(id),
    ico_officer     TEXT,
    email           TEXT,
    created_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_users_hospital ON users(hospital_id);
"""


# --------------------------------------------------------------------------
# In-memory dataset objects
# --------------------------------------------------------------------------
@dataclass
class Sens:
    antibiotic: str = ""
    category: str = ""
    result: str = ""
    mic: str = ""
    intrinsic: bool = False

    @property
    def resistant(self) -> bool:
        return self.result == "R"

    @property
    def non_susceptible(self) -> bool:
        return self.result in ("R", "I") and not self.intrinsic


@dataclass
class Iso:
    """One organism-isolate event, with its antibiogram attached."""
    id: int = 0
    report_id: int = 0
    patient_id: str = ""
    patient_name: str = ""
    ward: str = "Unspecified"
    room: str = ""
    specimen_type: str = "Unspecified"
    sample_date: str = ""
    organism_raw: str = ""
    pathogen: str = ""
    confidence: float = 0.0
    match_method: str = ""
    matched_phrase: str = ""
    other_label: str = ""
    markers: list[str] = field(default_factory=list)
    genus_only: bool = False
    suppressed: bool = False
    result_flag: str = "positive"
    notes: str = ""
    source_row: int = 0
    sensitivities: list[Sens] = field(default_factory=list)

    # ---- derived ---------------------------------------------------------
    @property
    def day(self) -> date | None:
        try:
            return date.fromisoformat(self.sample_date)
        except (TypeError, ValueError):
            return None

    @property
    def month(self) -> str:
        return (self.sample_date or "")[:7]

    @property
    def acquisition_key(self) -> tuple[str, str]:
        """(patient, pathogen) - one episode of acquisition."""
        return (self.patient_id, self.pathogen)

    def acquired(self, antibiotic: str) -> Sens | None:
        for s in self.sensitivities:
            if s.antibiotic == antibiotic:
                return s
        return None

    @property
    def tested_acquired_categories(self) -> set[str]:
        return {s.category for s in self.sensitivities
                if s.result in ("S", "I", "R") and not s.intrinsic}

    @property
    def resistant_categories(self) -> set[str]:
        return {s.category for s in self.sensitivities
                if s.result == "R" and not s.intrinsic}

    @property
    def non_susceptible_categories(self) -> set[str]:
        return {s.category for s in self.sensitivities if s.non_susceptible}

    @property
    def resistant_drugs(self) -> list[str]:
        return [s.antibiotic for s in self.sensitivities if s.resistant]

    @property
    def profile(self) -> str:
        return "|".join(f"{s.antibiotic}:{s.result}"
                        for s in sorted(self.sensitivities, key=lambda x: x.antibiotic)
                        if s.result)


# --------------------------------------------------------------------------
# Connection management
# --------------------------------------------------------------------------
def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def init_accounts(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_ACCOUNTS)
    conn.commit()


# --------------------------------------------------------------------------
# Users / authentication
# --------------------------------------------------------------------------
def create_user(conn: sqlite3.Connection, *, username: str, password_hash: str,
                hospital_id: int, ico_officer: str = "", email: str = "") -> int:
    """Insert an account owned by ``hospital_id``. ``username`` is lower-cased."""
    cur = conn.execute(
        """INSERT INTO users (username, password_hash, hospital_id,
             ico_officer, email, created_at) VALUES (?,?,?,?,?,?)""",
        (username, password_hash, hospital_id, ico_officer, email,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return cur.lastrowid


_USER_JOIN = """
    SELECT u.*, h.name AS hospital_name, h.slug AS hospital_slug
    FROM users u JOIN hospitals h ON h.id = u.hospital_id
"""


def get_user(conn: sqlite3.Connection, user_id: int):
    return conn.execute(_USER_JOIN + " WHERE u.id=?", (user_id,)).fetchone()


def find_user(conn: sqlite3.Connection, identifier: str):
    """Resolve a login by username or e-mail (case-insensitive)."""
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    return conn.execute(
        _USER_JOIN + " WHERE u.username=? OR u.email=?", (ident, ident)
    ).fetchone()


def username_taken(conn: sqlite3.Connection, username: str) -> bool:
    ident = (username or "").strip().lower()
    return conn.execute(
        "SELECT 1 FROM users WHERE username=?", (ident,)).fetchone() is not None


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def update_user_profile(conn: sqlite3.Connection, user_id: int, *,
                        ico_officer: str | None = None,
                        email: str | None = None) -> None:
    """Partial update of the mutable per-user profile columns.

    ``None`` means "leave this column alone"; empty strings are stored as-is so
    an officer can deliberately clear one. Tenancy (``hospital_id``), the login
    handle (``username``) and the password hash are never touched here - the
    Profile page has no business changing which workspace a user belongs to.
    """
    sets, vals = [], []
    if ico_officer is not None:
        sets.append("ico_officer=?"); vals.append(ico_officer)
    if email is not None:
        sets.append("email=?"); vals.append(email)
    if not sets:
        return
    vals.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def set_password_hash(conn: sqlite3.Connection, user_id: int,
                      password_hash: str) -> None:
    """Overwrite the account's password hash. Callers must have already
    verified the previous password and hashed the new one (werkzeug's
    ``generate_password_hash``)."""
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (password_hash, user_id))
    conn.commit()


def email_in_use_by_other(conn: sqlite3.Connection, email: str,
                          exclude_user_id: int) -> bool:
    """True when ``email`` already belongs to a different account.

    ``find_user`` resolves a login by ``username`` OR ``email``, so two
    accounts sharing one address would make sign-in ambiguous. The Profile
    page checks this before persisting an e-mail edit.
    """
    if not email:
        return False
    return conn.execute(
        "SELECT 1 FROM users WHERE email=? AND id<>? LIMIT 1",
        (email.lower(), exclude_user_id)).fetchone() is not None


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
def create_report(conn: sqlite3.Connection, *, filename: str, file_type: str = "",
                  file_size: int = 0, sha256: str = "", source: str = "upload",
                  stored_name: str = "", layout: str = "", status: str = "pending",
                  message: str = "", rows_read: int = 0, rows_skipped: int = 0,
                  parse_log: str = "") -> int:
    cur = conn.execute(
        """INSERT INTO reports (filename, stored_name, file_type, file_size, sha256,
             source, uploaded_at, rows_read, rows_skipped, layout, status, message,
             parse_log)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (filename, stored_name, file_type, file_size, sha256, source,
         datetime.now().isoformat(timespec="seconds"), rows_read, rows_skipped,
         layout, status, message, parse_log))
    conn.commit()
    return int(cur.lastrowid)


def update_report(conn: sqlite3.Connection, report_id: int, **cols) -> None:
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE reports SET {sets} WHERE id = ?", (*cols.values(), report_id))
    conn.commit()


def insert_records(conn: sqlite3.Connection, report_id: int, records) -> tuple[int, int]:
    """Persist parsed :class:`~bioguard.parsing.common.LabRecord` objects."""
    n_iso = n_sen = 0
    for rec in records:
        pathogen = rec.pathogen or ""
        intrinsic_set = PATHOGENS[pathogen].intrinsic if pathogen in PATHOGENS else frozenset()
        cur = conn.execute(
            """INSERT OR IGNORE INTO isolates
               (report_id, patient_id, patient_name, ward, room, specimen_type,
                sample_date, sample_month, organism_raw, pathogen, confidence,
                match_method, matched_phrase, other_label, markers, genus_only,
                suppressed, result_flag, notes, source_row)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (report_id, rec.patient_id, rec.patient_name, rec.ward, rec.room,
             rec.specimen_type, rec.sample_date, (rec.sample_date or "")[:7],
             rec.organism_raw, pathogen, rec.confidence, rec.match_method,
             rec.matched_phrase, rec.other_label, json.dumps(rec.markers),
             1 if rec.genus_only else 0, 1 if rec.suppressed else 0,
             rec.result_flag, rec.notes, rec.source_row))
        if cur.rowcount == 0:
            row = conn.execute(
                """SELECT id FROM isolates WHERE report_id=? AND patient_id=?
                   AND pathogen=? AND specimen_type=? AND sample_date=?""",
                (report_id, rec.patient_id, pathogen, rec.specimen_type,
                 rec.sample_date)).fetchone()
            iso_id = int(row["id"]) if row else 0
        else:
            iso_id = int(cur.lastrowid)
            n_iso += 1
        if not iso_id:
            continue
        for s in rec.sensitivities:
            conn.execute(
                """INSERT INTO sensitivities
                   (isolate_id, antibiotic_raw, antibiotic, category, result, mic,
                    intrinsic) VALUES (?,?,?,?,?,?,?)""",
                (iso_id, s.antibiotic_raw, s.antibiotic,
                 s.category or category_of(s.antibiotic), s.result, s.mic,
                 1 if (s.antibiotic in intrinsic_set) else 0))
            n_sen += 1
    conn.commit()
    return n_iso, n_sen


def delete_report(conn: sqlite3.Connection, report_id: int) -> bool:
    cur = conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
    conn.commit()
    return cur.rowcount > 0


def reset_database(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sensitivities")
    conn.execute("DELETE FROM isolates")
    conn.execute("DELETE FROM reports")
    conn.execute("DELETE FROM meta")
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)",
                 (key, str(value)))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
_SELECT_ISOLATES = """
    SELECT i.*, r.filename AS report_filename, r.source AS report_source
    FROM isolates i LEFT JOIN reports r ON r.id = i.report_id
"""


def load_dataset(conn: sqlite3.Connection, *, target_only: bool = True,
                 date_from: str = "", date_to: str = "", ward: str = "",
                 pathogen: str = "", include_suppressed: bool = False) -> list[Iso]:
    """Materialise isolates (with antibiograms) into memory."""
    where: list[str] = []
    params: list = []
    if target_only:
        where.append("i.pathogen IS NOT NULL AND i.pathogen <> ''")
    if not include_suppressed:
        where.append("i.suppressed = 0")
    if date_from:
        where.append("i.sample_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("i.sample_date <= ?")
        params.append(date_to)
    if ward:
        where.append("i.ward = ?")
        params.append(ward)
    if pathogen:
        where.append("i.pathogen = ?")
        params.append(pathogen)

    sql = _SELECT_ISOLATES
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.sample_date, i.id"

    rows = conn.execute(sql, params).fetchall()
    by_id: dict[int, Iso] = {}
    order: list[int] = []
    for r in rows:
        try:
            markers = json.loads(r["markers"] or "[]")
        except json.JSONDecodeError:
            markers = []
        iso = Iso(
            id=r["id"], report_id=r["report_id"] or 0, patient_id=r["patient_id"],
            patient_name=r["patient_name"] or "", ward=r["ward"] or "Unspecified",
            room=r["room"] or "", specimen_type=r["specimen_type"] or "Unspecified",
            sample_date=r["sample_date"] or "", organism_raw=r["organism_raw"] or "",
            pathogen=r["pathogen"] or "", confidence=r["confidence"] or 0.0,
            match_method=r["match_method"] or "", matched_phrase=r["matched_phrase"] or "",
            other_label=r["other_label"] or "", markers=markers,
            genus_only=bool(r["genus_only"]), suppressed=bool(r["suppressed"]),
            result_flag=r["result_flag"] or "positive", notes=r["notes"] or "",
            source_row=r["source_row"] or 0,
        )
        by_id[iso.id] = iso
        order.append(iso.id)

    if not by_id:
        return []
    ids = list(by_id)
    sens_rows = []
    # Chunk to stay well below SQLite's variable limit.
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        sens_rows += conn.execute(
            f"""SELECT * FROM sensitivities WHERE isolate_id IN
                ({','.join('?' * len(chunk))}) ORDER BY antibiotic""",
            chunk).fetchall()
    for s in sens_rows:
        iso = by_id.get(s["isolate_id"])
        if iso is not None:
            iso.sensitivities.append(Sens(
                antibiotic=s["antibiotic"] or "", category=s["category"] or "",
                result=s["result"] or "", mic=s["mic"] or "",
                intrinsic=bool(s["intrinsic"])))
    return [by_id[i] for i in order]


def list_reports(conn: sqlite3.Connection, limit: int = 50, offset: int = 0):
    return conn.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM isolates i WHERE i.report_id = r.id
                        AND i.pathogen <> '' AND i.suppressed = 0) AS target_hits,
                  (SELECT COUNT(*) FROM isolates i WHERE i.report_id = r.id) AS rows_total
           FROM reports r ORDER BY r.id DESC LIMIT ? OFFSET ?""",
        (limit, offset)).fetchall()


def count_reports(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"])


def get_report(conn: sqlite3.Connection, report_id: int):
    return conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()


def report_isolates(conn: sqlite3.Connection, report_id: int) -> list[Iso]:
    rows = conn.execute(
        "SELECT * FROM isolates WHERE report_id=? ORDER BY sample_date, id",
        (report_id,)).fetchall()
    out: list[Iso] = []
    for r in rows:
        try:
            markers = json.loads(r["markers"] or "[]")
        except json.JSONDecodeError:
            markers = []
        iso = Iso(id=r["id"], report_id=r["report_id"], patient_id=r["patient_id"],
                  patient_name=r["patient_name"] or "", ward=r["ward"] or "",
                  room=r["room"] or "", specimen_type=r["specimen_type"] or "",
                  sample_date=r["sample_date"] or "", organism_raw=r["organism_raw"] or "",
                  pathogen=r["pathogen"] or "", confidence=r["confidence"] or 0,
                  match_method=r["match_method"] or "", other_label=r["other_label"] or "",
                  markers=markers, genus_only=bool(r["genus_only"]),
                  suppressed=bool(r["suppressed"]), result_flag=r["result_flag"] or "",
                  notes=r["notes"] or "", source_row=r["source_row"] or 0)
        out.append(iso)
    if out:
        ids = [o.id for o in out]
        srows = conn.execute(
            f"""SELECT * FROM sensitivities WHERE isolate_id IN
                ({','.join('?' * len(ids))}) ORDER BY antibiotic""", ids).fetchall()
        lookup = {o.id: o for o in out}
        for s in srows:
            lookup[s["isolate_id"]].sensitivities.append(Sens(
                antibiotic=s["antibiotic"] or "", category=s["category"] or "",
                result=s["result"] or "", mic=s["mic"] or "",
                intrinsic=bool(s["intrinsic"])))
    return out


def database_counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM reports) AS reports,
             (SELECT COUNT(*) FROM isolates) AS isolates,
             (SELECT COUNT(*) FROM isolates WHERE pathogen <> '' AND suppressed = 0)
                 AS targets,
             (SELECT COUNT(DISTINCT patient_id) FROM isolates
                 WHERE pathogen <> '' AND suppressed = 0) AS patients,
             (SELECT COUNT(DISTINCT ward) FROM isolates
                 WHERE pathogen <> '' AND suppressed = 0) AS wards,
             (SELECT COUNT(*) FROM sensitivities) AS sensitivities,
             (SELECT MIN(sample_date) FROM isolates WHERE sample_date <> '') AS first_date,
             (SELECT MAX(sample_date) FROM isolates WHERE sample_date <> '') AS last_date
        """).fetchone()
    return dict(row)


def available_wards(conn: sqlite3.Connection) -> list[str]:
    return [r["ward"] for r in conn.execute(
        "SELECT DISTINCT ward FROM isolates WHERE ward <> '' ORDER BY ward").fetchall()]


def available_pathogens(conn: sqlite3.Connection) -> list[str]:
    return [r["pathogen"] for r in conn.execute(
        "SELECT DISTINCT pathogen FROM isolates WHERE pathogen <> '' ORDER BY pathogen"
    ).fetchall()]
