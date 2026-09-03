"""
One-time migration from the legacy single shared database to per-hospital
workspaces.

Policy (agreed): the legacy DB's *accounts* map reliably to hospitals, but its
surveillance data does not unless a per-report attribution column exists. This
module therefore:

  * refuses to run on a corrupt legacy DB (integrity_check must pass);
  * makes a verified backup BEFORE writing anything;
  * probes whether the data can be reliably partitioned by hospital;
  * when it cannot (the common case for a shared/demo database), provisions an
    EMPTY workspace per account and PRESERVES the legacy file and its orphaned
    uploads untouched - it never guesses an owner;
  * is idempotent and transactional, so a crash or re-run is harmless;
  * never deletes the legacy source (cleanup is a separate manual step).

Nothing here seeds demo/synthetic data into any tenant.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import database, tenancy

MIGRATION_VERSION = 1


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _marked_done(reg: sqlite3.Connection) -> bool:
    return int(reg.execute("PRAGMA user_version").fetchone()[0]) >= MIGRATION_VERSION


def _mark_done(reg: sqlite3.Connection, backup: Path | None) -> None:
    reg.execute(f"PRAGMA user_version={MIGRATION_VERSION}")
    reg.commit()


# --------------------------------------------------------------------------
# Legacy inspection
# --------------------------------------------------------------------------
def partitionability_probe(conn: sqlite3.Connection) -> dict:
    """Decide whether legacy surveillance data can be reliably split by hospital.

    Reliable only if a report carries a real attribution key that names a
    hospital account. A shared synthetic demo dataset with no such key is NOT
    partitionable, and must not be guessed onto any tenant.
    """
    cols = _table_columns(conn, "reports")
    keys = sorted(c for c in cols
                  if "hospital" in c.lower() or c.lower() in ("tenant_id", "org_id"))
    if keys:
        return {"partitionable": True, "column": keys[0],
                "reason": f"found attribution column(s): {keys}"}
    return {"partitionable": False, "column": None,
            "reason": "reports carry no hospital column or FK to users"}


def _legacy_accounts(conn: sqlite3.Connection) -> list[dict]:
    """The legacy ``users`` rows -> future hospitals, or [] if absent/unreadable."""
    cols = _table_columns(conn, "users")
    if "username" not in cols or "password_hash" not in cols:
        return []
    want = [c for c in ("username", "password_hash", "hospital_name",
                        "ico_officer", "email") if c in cols]
    out = []
    for r in conn.execute(f"SELECT {', '.join(want)} FROM users ORDER BY rowid"):
        rec = dict(r)
        if not rec.get("username"):
            continue
        out.append(rec)
    return out


def _orphan_uploads(app, legacy_conn: sqlite3.Connection) -> list[str]:
    up = Path(app.config["UPLOAD_DIR"])
    if not up.exists():
        return []
    live = set()
    try:
        live = {r["stored_name"] for r in legacy_conn.execute(
            "SELECT stored_name FROM reports WHERE stored_name IS NOT NULL")}
    except sqlite3.Error:
        pass
    return [p.name for p in up.iterdir()
            if p.is_file() and p.name not in live and not p.name.startswith(".")]


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------
def _backup(legacy: Path, app, log) -> Path | None:
    backups = Path(app.config["BACKUPS_DIR"])
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"bioguard-legacy-{_stamp()}.db"
    try:
        shutil.copy2(legacy, dest)
        for suffix in ("-wal", "-shm"):
            side = legacy.with_name(legacy.name + suffix)
            if side.exists():
                shutil.copy2(side, dest.with_name(dest.name + suffix))
    except OSError as exc:
        log(f"ABORT: could not back up legacy DB: {exc}")
        return None
    check = database.connect(dest)
    try:
        ok = _integrity_ok(check)
    finally:
        check.close()
    if not ok:
        log(f"ABORT: backup failed integrity_check: {dest}")
        return None
    log(f"Backup written and verified: {dest}")
    return dest


# --------------------------------------------------------------------------
# Partitionable data copy (forward-compatible; not used by the demo DB)
# --------------------------------------------------------------------------
def _migrate_data_partitioned(reg, legacy: Path, log) -> None:
    src = _open_readonly(legacy)
    try:
        col = partitionability_probe(src)["column"]
        hospitals = tenancy.list_hospitals(reg)
        for h in hospitals:
            tgt = database.connect(tenancy.data_db_path(h["slug"]))
            database.init_db(tgt)
            tgt.execute("ATTACH DATABASE ? AS src",
                        (str(legacy.resolve().as_posix()),))
            try:
                tgt.execute(
                    f"INSERT INTO reports SELECT * FROM src.reports "
                    f"WHERE src.reports.{col} = ?", (h["name"],))
                tgt.execute(
                    "INSERT INTO isolates SELECT * FROM src.isolates WHERE report_id IN "
                    "(SELECT id FROM src.reports WHERE src.reports." + col + " = ?)",
                    (h["name"],))
                tgt.execute(
                    "INSERT INTO sensitivities SELECT * FROM src.sensitivities WHERE "
                    "isolate_id IN (SELECT i.id FROM src.isolates i JOIN src.reports r "
                    "ON r.id = i.report_id WHERE r." + col + " = ?)", (h["name"],))
                tgt.commit()
                n = tgt.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
                log(f"  data -> {h['slug']}: {n} report(s)")
            finally:
                tgt.execute("DETACH DATABASE src")
                tgt.close()
    finally:
        src.close()


# --------------------------------------------------------------------------
# Verification gate
# --------------------------------------------------------------------------
def _verify(reg, accounts: list[dict], log) -> bool:
    ok = True
    for acc in accounts:
        row = reg.execute(
            "SELECT h.slug FROM users u JOIN hospitals h ON h.id=u.hospital_id "
            "WHERE u.username=?", (acc["username"],)).fetchone()
        if row is None:
            log(f"  VERIFY FAIL: no workspace for {acc['username']}")
            ok = False
            continue
        if not tenancy.data_db_path(row["slug"]).exists():
            log(f"  VERIFY FAIL: missing tenant DB for {row['slug']}")
            ok = False
    return ok


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def run(app, *, dry_run: bool = False) -> dict:
    lines: list[str] = []
    log = lines.append

    accounts_path = Path(app.config["ACCOUNTS_DB_PATH"])
    reg = database.connect(accounts_path)
    database.init_accounts(reg)
    try:
        if tenancy.count_hospitals(reg) > 0 or _marked_done(reg):
            log("Registry already provisioned; nothing to migrate.")
            return {"status": "noop", "lines": lines}

        legacy = Path(app.config["DB_PATH"])
        if not legacy.exists():
            log(f"No legacy database at {legacy}; nothing to migrate.")
            return {"status": "noop", "lines": lines}

        # 1. Pre-flight integrity (read-only) before touching anything.
        leg = _open_readonly(legacy)
        try:
            if not _integrity_ok(leg):
                log(f"ABORT: legacy DB failed integrity_check: {legacy}")
                return {"status": "aborted", "lines": lines}
            accounts = _legacy_accounts(leg)
            probe = partitionability_probe(leg)
            orphans = _orphan_uploads(app, leg)
        finally:
            leg.close()

        log(f"Partitionable: {probe['partitionable']} ({probe['reason']})")
        log(f"Legacy accounts to provision: {len(accounts)}")
        if not probe["partitionable"]:
            log(f"Unattributable uploads preserved: {len(orphans)} file(s)")

        if not accounts:
            log("No accounts found in the legacy DB; nothing provisioned.")
            return {"status": "noop", "lines": lines}

        # 2. Backup first.
        backup = None
        if not dry_run:
            backup = _backup(legacy, app, log)
            if backup is None:
                return {"status": "aborted", "lines": lines}
        else:
            log("DRY RUN: no changes will be written.")
            slugs = ", ".join(tenancy.slugify((a.get('hospital_name') or '').strip()
                                               or a["username"]) for a in accounts)
            log(f"Would provision workspaces: {slugs}")
            return {"status": "dry-run", "lines": lines}

        # 3/4/5. Provision an EMPTY workspace per account (transactional, idempotent).
        for acc in accounts:
            name = (acc.get("hospital_name") or "").strip() or "Unnamed hospital"
            try:
                hosp, uid = tenancy.provision_workspace(
                    reg, hospital_name=name, username=acc["username"].lower(),
                    password_hash=acc["password_hash"],
                    email=(acc.get("email") or ""),
                    ico_officer=(acc.get("ico_officer") or ""))
                log(f"  provisioned {hosp['slug']:<28} <- {acc['username']}")
            except sqlite3.IntegrityError as exc:
                reg.rollback()
                log(f"  SKIP {acc['username']}: {exc}")

        # Data: split only when reliable, otherwise preserve legacy untouched.
        if probe["partitionable"]:
            _migrate_data_partitioned(reg, legacy, log)
        else:
            log("Shared/synthetic data is NOT attributed to any hospital and stays "
                "in the legacy DB; every new workspace is empty (opt-in demo only).")

        # 7. Mark done, then verify.
        _mark_done(reg, backup)
        good = _verify(reg, accounts, log)
        log(f"Legacy source preserved at {legacy} (not deleted).")
        log("Migration verified." if good else "Migration completed with warnings.")
        return {"status": "ok" if good else "warn", "lines": lines,
                "backup": str(backup), "accounts": len(accounts)}
    finally:
        reg.close()


def auto_migrate_if_needed(app) -> None:
    """Non-destructive startup pass: only acts when the registry is empty AND a
    legacy DB with accounts exists. Never seeds data. Safe to call every boot."""
    try:
        accounts_path = Path(app.config["ACCOUNTS_DB_PATH"])
        legacy = Path(app.config["DB_PATH"])
        if not legacy.exists():
            return
        reg = database.connect(accounts_path)
        try:
            database.init_accounts(reg)
            if tenancy.count_hospitals(reg) > 0 or _marked_done(reg):
                return
        finally:
            reg.close()
        # Provisioning resolves workspace paths through ``tenancy``, which reads
        # the *live app config*. ``create_app`` calls this before any request, so
        # run inside an app context to make sure tenants are created under THIS
        # app's HOSPITALS_DIR (and a test's temp dir) rather than a stale default.
        from flask import has_app_context
        if has_app_context():
            run(app)
        else:
            with app.app_context():
                run(app)
    except Exception as exc:  # never block app startup on migration issues
        app.logger.exception("auto-migration skipped: %s", exc)
