"""
HTML pages.

Each view resolves the active scope, loads the dataset once and hands an
:class:`~bioguard.views.Insights` to the template. Pages are fully rendered
server-side; the JavaScript layer only draws charts and refreshes them, so the
application still works with scripting disabled.
"""

from __future__ import annotations

from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, send_from_directory, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import config
from .. import database, get_accounts_db, get_db, ingest, tenancy
from ..antibiotics import CATEGORIES
from ..scope import current_scope, dataset as load_dataset, resolve_scope
from ..views import Insights
from .auth import current_user as _signed_in, logout_user as _end_session

bp = Blueprint("main", __name__)

SAMPLE_DIR = Path(config.BASE_DIR) / "samples"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _notif_flags(conn) -> dict:
    """Read the workspace's alert preferences from the per-hospital ``meta`` table.

    Two independent switches, both default to ON so behaviour is unchanged for
    every existing workspace:
      ``in_app``      hides / shows alert UI on the Dashboard and Alerts page.
      ``generation``  when off, the analysis's alert output is emptied before
                      it reaches any page or feed (``findings``,
                      ``amr_summary['alerts']``, ``alert_center``).
    Stored as ``'1'`` / ``'0'``; anything else is treated as on. Only reads
    the ``meta`` table; does not touch surveillance data.
    """
    return {
        "in_app": database.get_meta(conn, "notif_in_app", "1") != "0",
        "generation": database.get_meta(conn, "notif_generation", "1") != "0",
    }

_EMPTY_ALERT_CENTER = {
    "active": False, "assessment": None, "level": "None", "color": "#64748b",
    "pathogen": "", "pathogen_display": "", "organism_full": "",
    "ward": "", "other_wards": 0, "cases": 0, "patients": 0,
    "excess_ratio": 0, "mdr_rate": 0, "score": 0,
    "trend": [], "trend_peak": 0, "trend_total": 0,
    "agents": [], "reasons": [], "actions": [],
    "recommendation": "", "subject": "", "body": "",
    "mailto": "", "whatsapp": "", "counts": {"High": 0, "Medium": 0, "Low": 0},
}


def _apply_generation_flag(view, gen_on: bool) -> None:
    """When generation is off, pre-seed the cached alert outputs so every
    consumer (Dashboard ``alerts``/``findings``, Alerts page ``alert_center``,
    the AMR alert queue, and the API view) sees nothing generated. The
    underlying analytics (outbreak scoring, MDR/XDR/PDR, trends) are left
    untouched - only the alert assembly step is short-circuited.
    """
    if gen_on:
        return
    # Populate the ``cached_property`` slots on the instance so no downstream
    # code re-derives a non-empty value.
    view.__dict__["findings"] = []
    view.__dict__["alert_center"] = dict(_EMPTY_ALERT_CENTER)
    summ = view.amr_summary if isinstance(view.amr_summary, dict) else None
    if summ is not None:
        summ["alerts"] = []
        # ``last_line_exhausted`` is a different signal; leave it alone.


def insights(target_only: bool = True):
    """Connection, scope and the view model for the current request."""
    conn = get_db()
    scope = current_scope()
    isolates = load_dataset(conn, scope, target_only=True)
    extra = isolates if not scope.include_nontarget else None
    if extra is None:
        extra = load_dataset(conn, scope, target_only=False)
    view = Insights(conn=conn, scope=scope, isolates=isolates, all_rows=extra)
    _apply_generation_flag(view, _notif_flags(conn)["generation"])
    return conn, scope, view


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    """Read a bounded integer argument without letting junk raise a 500."""
    try:
        return min(hi, max(lo, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@bp.get("/")
def dashboard():
    _conn, scope, view = insights()
    summ = view.outbreak_summary
    return render_template(
        "dashboard.html",
        view=view, kpis=view.kpis, status=view.status_line,
        high=summ["high"], medium=summ["medium"], low=summ["low"],
        counts=summ["counts"], findings=view.findings[:8],
        clusters=view.cluster_rows[:10],
        ward_clusters=view.recent_ward_clusters[:10],
        risk_units=view.risk_units[:12],
        chart_payload=view.chart_payload,
        alerts=view.amr_summary["alerts"],
        page="dashboard",
    )


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------
@bp.get("/trends")
def trends_page():
    _conn, scope, view = insights()
    return render_template(
        "trends.html", view=view, kpis=view.kpis,
        rows=view.trend_rows, chart_payload=view.chart_payload,
        wards=view.ward_rows, clusters=view.cluster_rows,
        ward_clusters=view.ward_cluster_rows,
        rising=[r for r in view.trend_rows if r["direction"] in ("rising", "emerging")],
        multi_patient=[r for r in view.trend_rows if r["recent_patients"] >= 2],
        multi_ward=[r for r in view.trend_rows if r["recent_wards"] >= 2],
        page="trends",
    )


@bp.get("/ward-heatmap")
def ward_heatmap_page():
    """Dedicated Ward Heatmap page.

    Reaches the same tenant-scoped :func:`insights` view the ``/trends`` page
    uses and renders the shared ``_ward_heatmap.html`` partial. It passes the
    already-computed ``view.ward_rows`` (-> ``trends.ward_matrix``) and
    ``view.chart_payload`` verbatim, so no analytics are recalculated and no
    extra query is issued; only the sidebar ``page`` key differs. Setting
    ``page='ward_heatmap'`` is what makes the sidebar highlight the correct
    item server-side, with no JavaScript.
    """
    _conn, scope, view = insights()
    return render_template(
        "ward_heatmap.html", view=view, kpis=view.kpis,
        wards=view.ward_rows, chart_payload=view.chart_payload,
        page="ward_heatmap",
    )


@bp.get("/amr")
def amr_page():
    _conn, scope, view = insights()
    summ = view.amr_summary
    min_tests = config.MIN_ISOLATES_FOR_RESISTANCE_RATE
    return render_template(
        "amr.html", view=view, kpis=view.kpis, amr=summ,
        chart_payload=view.chart_payload, movements=view.amr_trends["movements"],
        monthly=view.amr_trends,
        rising=[m for m in view.amr_trends["movements"] if m["direction"] == "rising"
                and m["tests_recent"] >= min_tests],
        falling=[m for m in view.amr_trends["movements"] if m["direction"] == "falling"
                 and m["tests_recent"] >= min_tests],
        agents=summ["highest_resistance"],
        page="amr",
    )


@bp.get("/outbreak")
def outbreak_page():
    _conn, scope, view = insights()
    summ = view.outbreak_summary
    selected = request.args.get("pathogen", "")
    ranked = summ["ranked"]
    return render_template(
        "outbreak.html", view=view, kpis=view.kpis, status=view.status_line,
        ranked=ranked, counts=summ["counts"],
        clusters=view.cluster_rows, ward_clusters=view.ward_cluster_rows,
        risk_units=view.risk_units, chart_payload=view.chart_payload,
        selected=next((a for a in ranked if a["pathogen"] == selected), None),
        weights=config.RISK_WEIGHTS,
        thresholds={"high": config.RISK_HIGH_THRESHOLD,
                    "medium": config.RISK_MEDIUM_THRESHOLD},
        page="outbreak",
    )


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
@bp.get("/alerts")
def alerts_page():
    _conn, scope, view = insights()
    summ = view.outbreak_summary
    return render_template(
        "alerts.html", view=view, kpis=view.kpis, status=view.status_line,
        alert=view.alert_center, counts=summ["counts"],
        high=summ["high"], risk_units=view.risk_units[:6],
        chart_payload=view.chart_payload, page="alerts",
    )


@bp.post("/alerts/notify")
def alerts_notify():
    """Log that the Infection Control Team was notified and bounce back.

    There is no mail server wired into an air-gapped hospital deployment, so the
    action records the acknowledgement on the audit console and hands the actual
    message to the operator through the mailto / WhatsApp links on the page.
    """
    _conn, _scope, view = insights()
    alert = view.alert_center
    if alert.get("assessment"):
        a = alert["assessment"]
        current_app.logger.info(
            "IPC notification raised: %s on %s at %s risk (score %s)",
            a["pathogen_display"], alert["ward"], a["level"], a["score"])
        flash(
            f"Infection Control Team notified about {a['pathogen_display']} on "
            f"{alert['ward']} ({a['level']} risk, score {a['score']}). A copy of "
            "the alert summary has been prepared for email and WhatsApp below.",
            "success")
    else:
        flash("No active outbreak alert to notify about right now.", "warning")
    return redirect(url_for("main.alerts_page"))


# --------------------------------------------------------------------------
# Per-pathogen detail
# --------------------------------------------------------------------------
@bp.get("/pathogen/<key>")
def pathogen_page(key: str):
    _conn, scope, view = insights()
    detail = view.pathogen_detail(key)
    if detail is None:
        abort(404, f"Unknown pathogen '{key}'.")
    return render_template("pathogen.html", view=view, kpis=view.kpis,
                           d=detail, chart_payload=view.scoped_chart(key), page="trends")


# --------------------------------------------------------------------------
# Patients / isolates browser
# --------------------------------------------------------------------------
@bp.get("/patients")
def patients_page():
    _conn, scope, view = insights()
    return render_template("patients.html", view=view, kpis=view.kpis,
                           patients=view.patient_rows, page="trends")


@bp.get("/isolates")
def isolates_page():
    _conn, _scope, view = insights()
    limit = _int_arg("limit", 100, 10, 500)
    offset = _int_arg("offset", 0, 0, 1_000_000)
    search = request.args.get("q", "")
    return render_template(
        "isolates.html", view=view, kpis=view.kpis, table=view.isolate_rows(
            limit=limit, offset=offset, search=search),
        others=view.others, search=search, limit=limit, offset=offset,
        page="lab_reports")


# --------------------------------------------------------------------------
# Upload + report audit
# --------------------------------------------------------------------------
@bp.get("/upload")
def upload_page():
    conn = get_db()
    return render_template("upload.html", conn=conn,
                           reports=database_list(conn, 8),
                           samples=available_samples(),
                           counts=database_counts(conn),
                           max_mb=current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
                           allowed=sorted(config.ALLOWED_EXTENSIONS),
                           page="upload")


@bp.post("/upload")
def upload_form():
    """No-JavaScript upload path: same service, but a redirect with flashes."""
    from ..scope import dataset_cache_reset
    conn = get_db()
    files = request.files.getlist("file") + request.files.getlist("files[]")
    files = [f for f in files if f and f.filename]
    allow_dup = request.form.get("allow_duplicate") == "1"
    if not files:
        flash("Choose at least one CSV or PDF lab report to upload.", "warning")
        return redirect(url_for("main.upload_page"))

    ok = 0
    for fs in files:
        res = ingest.ingest_upload(
            conn, fs, upload_dir=tenancy.current_upload_dir(),
            allow_duplicate=allow_dup)
        dataset_cache_reset()
        _flash_result(res)
        ok += 1 if res.isolates else 0
    return redirect(url_for("main.dashboard") if ok
                    else url_for("main.upload_page"))


def _flash_result(res) -> None:
    kind = {"imported": "success", "duplicate": "warning",
            "empty": "warning", "error": "danger"}[res.status]
    message = f"{res.filename} - {res.message}"
    if res.errors:
        message += " " + "; ".join(res.errors[:2])
    if res.report_id and res.status not in ("duplicate", "error"):
        message += f" [report #{res.report_id}]"
    flash(message, kind)


@bp.get("/reports")
def reports_page():
    conn = get_db()
    page_no = _int_arg("page", 1, 1, 10000)
    per = config.DEFAULT_PAGE_SIZE
    total = database_counts(conn)
    return render_template(
        "reports.html", conn=conn,
        reports=database_list(conn, per, (page_no - 1) * per),
        total=total["reports"], page_no=page_no, per=per,
        pages=max(1, -(-total["reports"] // per)),
        counts=total, page="reports")


@bp.get("/reports/<int:report_id>")
def report_page(report_id: int):
    conn = get_db()
    report = database.get_report(conn, report_id)
    if report is None:
        abort(404, f"There is no report #{report_id}.")
    rows = database.report_isolates(conn, report_id)
    # A year-end LIS export can carry thousands of rows, and this page renders
    # each of them twice - once as an isolate, once as an antibiogram. Both tabs
    # share one offset because both are ordered by sample date and id.
    per = _int_arg("rows", config.DEFAULT_PAGE_SIZE * 4, 10, 500)
    offset = _int_arg("offset", 0, 0, max(0, len(rows)))
    pages = max(1, -(-len(rows) // per))
    offset = min(offset, (pages - 1) * per)
    return render_template(
        "report.html", report=report, isolates=rows[offset:offset + per],
        total_rows=len(rows), offset=offset, per=per, pages=pages,
        summary={
            "tracked": sum(1 for r in rows if r.pathogen and not r.suppressed),
            "nontarget": sum(1 for r in rows if not r.pathogen),
            "suppressed": sum(1 for r in rows if r.suppressed),
            "antibiograms": sum(1 for r in rows if r.sensitivities),
            "patients": len({r.patient_id for r in rows}),
            "wards": len({r.ward for r in rows if r.ward}),
        },
        log=(report["parse_log"] or "").splitlines(),
        categories=CATEGORIES,
        counts=database.database_counts(conn),
        page="reports")


@bp.get("/reports/<int:report_id>/download")
def report_download(report_id: int):
    conn = get_db()
    report = database.get_report(conn, report_id)
    if report is None:
        abort(404)
    path = ingest.uploaded_file_path(report, tenancy.current_upload_dir())
    if path is None:
        abort(404, "The original file is no longer stored on this server.")
    return send_from_directory(path.parent, path.name,
                               as_attachment=True,
                               download_name=report["filename"])


@bp.post("/reports/<int:report_id>/delete")
def report_delete(report_id: int):
    from ..scope import dataset_cache_reset
    conn = get_db()
    if database.delete_report(conn, report_id):
        dataset_cache_reset()
        flash(f"Report #{report_id} and every isolate parsed from it were deleted.",
              "success")
    else:
        flash(f"Report #{report_id} no longer exists.", "warning")
    return redirect(request.referrer or url_for("main.reports_page"))


@bp.get("/samples/<name>")
def sample_file(name: str):
    """Offer the bundled example reports so the upload flow can be tried at once."""
    safe = Path(name).name
    if not safe.endswith((".csv", ".tsv", ".txt", ".pdf")):
        abort(404)
    if not SAMPLE_DIR.exists() or not (SAMPLE_DIR / safe).exists():
        abort(404, f"No sample named '{safe}' is bundled with this install.")
    return send_from_directory(SAMPLE_DIR, safe, as_attachment=True)


def available_samples() -> list[dict]:
    if not SAMPLE_DIR.exists():
        return []
    out = []
    for p in sorted(SAMPLE_DIR.glob("*")):
        if p.suffix.lower() not in config.ALLOWED_EXTENSIONS:
            continue
        out.append({"name": p.name, "size": p.stat().st_size,
                    "kind": p.suffix.lstrip(".").upper()})
    return out


# --------------------------------------------------------------------------
# Scope filters + settings
# --------------------------------------------------------------------------
@bp.route("/filters", methods=["GET", "POST"])
def filters_apply():
    """Apply the filter bar, then return wherever the user came from.

    Accepted by GET as well as POST so the bar degrades to a plain HTML form.
    """
    resolve_scope(request.values)
    target = request.values.get("next") or url_for("main.dashboard")
    if not _is_local(target):
        target = url_for("main.dashboard")
    return redirect(target)


@bp.get("/filters/clear")
def filters_clear():
    resolve_scope({"clear": "1"})
    target = request.args.get("next") or ""
    if _is_local(target):
        return redirect(target)
    return redirect(request.referrer or url_for("main.dashboard"))


def _is_local(url: str) -> bool:
    """Open-redirect guard: only ever bounce back inside this app."""
    if not url or url.startswith("//"):
        return False
    if url.startswith("/"):
        return True
    host = request.host_url.rstrip("/")
    return url.startswith(host + "/") or url == host


@bp.get("/settings")
def settings_page():
    conn = get_db()
    from ..pathogens import PATHOGENS
    return render_template(
        "settings.html", conn=conn, counts=database_counts(conn),
        thresholds=[
            {"label": "Recent window", "value": f"{config.RECENT_WINDOW_DAYS} days",
             "help": "The \"current signal\" period every excess, cluster and "
                     "risk figure is measured over."},
            {"label": "Baseline window", "value": f"{config.BASELINE_WINDOW_DAYS} days",
             "help": "The equally long period immediately before the recent "
                     "window, used for the week-on-week comparison."},
            {"label": "Trend bucket", "value": f"{config.TREND_BUCKET_DAYS} days",
             "help": "Bucket width of the incidence series on the charts."},
            {"label": "Cluster window", "value": f"{config.CLUSTER_WINDOW_DAYS} days",
             "help": "Rolling window a transmission-cluster search looks back "
                     "from each isolate."},
            {"label": "Cluster excess factor", "value": f"{config.CLUSTER_EXCESS_FACTOR}\u00d7",
             "help": "A window must hold this multiple of the organism's own "
                     "expected count before it is reported at all."},
            {"label": "High risk at", "value": f"score {config.RISK_HIGH_THRESHOLD}",
             "help": "Weighted score at or above which an organism is called High."},
            {"label": "Medium risk at", "value": f"score {config.RISK_MEDIUM_THRESHOLD}",
             "help": "Weighted score at or above which an organism is called Medium."},
            {"label": "Min isolates for a resistance rate",
             "value": f"{config.MIN_ISOLATES_FOR_RESISTANCE_RATE} tests",
             "help": "Rates computed from fewer results than this are suppressed "
                     "as noise."},
            {"label": "AMR alert rate", "value": f"{config.AMR_ALERT_RATE * 100:.0f}%",
             "help": "Resistance at or above this level on a critical agent is "
                     "raised as an AMR alert."},
            {"label": "Worsening signal", "value": f"{config.AMR_RISE_ALERT_PP} points",
             "help": "A rise in resistance of at least this many percentage "
                     "points is reported as moving against you."},
        ],
        model={
            "cluster_min_patients": config.CLUSTER_MIN_PATIENTS,
            "ward_cluster_min_patients": config.WARD_CLUSTER_MIN_PATIENTS,
            "cluster_spread_min_patients": config.CLUSTER_SPREAD_MIN_PATIENTS,
            "mdr_categories": config.MDR_MIN_CATEGORIES_RESISTANT,
            "xdr_options": config.XDR_MAX_CATEGORIES_WITH_OPTION,
            "endemic_gate": config.RISK_ENDEMIC_GATE_MIN,
            "suppress_ratio": config.ENDEMIC_SUPPRESS_RATIO,
            "escalation_min_ratio": config.ESCALATION_MIN_RATIO,
            "escalation_min_score": config.ESCALATION_MIN_SCORE,
            "large_cluster": config.LARGE_WARD_CLUSTER_PATIENTS,
            "protected_cluster": config.PROTECTED_WARD_CLUSTER_PATIENTS,
            "spread_patients": config.SPREAD_CLUSTER_MIN_PATIENTS,
            "max_escalation": config.MAX_ESCALATION_STEPS,
            "baseline_bins": config.BASELINE_MIN_BINS,
            "saturation": config.RISK_SATURATION,
            "page_size": config.DEFAULT_PAGE_SIZE,
            "keep_uploads": bool(config.KEEP_UPLOADED_FILES),
            "max_upload_mb": int(config.Config.MAX_CONTENT_LENGTH) // (1024 * 1024),
        },
        weights=config.RISK_WEIGHTS,
        pathogens=[{"key": k, "name": PATHOGENS[k].name,
                    "short": PATHOGENS[k].short_name,
                    "aliases": len(PATHOGENS[k].aliases),
                    "gram": PATHOGENS[k].gram,
                    "threat": PATHOGENS[k].threat,
                    "mdro": PATHOGENS[k].mdro,
                    "public_health": PATHOGENS[k].public_health}
                   for k in PATHOGENS],
        protected_wards=sorted(config.HIGH_CONSEQUENCE_WARDS),
        samples=available_samples(),
        page="settings")


@bp.post("/settings/seed")
def settings_seed():
    from ..scope import dataset_cache_reset
    conn = get_db()
    res = ingest.seed_demo(conn, force=True)
    dataset_cache_reset()
    flash(res["message"], "success" if res["status"] == "seeded" else "warning")
    return redirect(url_for("main.dashboard"))


@bp.post("/settings/demo-off")
def settings_demo_off():
    from ..scope import dataset_cache_reset
    conn = get_db()
    n = ingest.wipe_demo(conn)
    dataset_cache_reset()
    flash(f"Removed {n:,} demo sensitivity rows and the demo report.", "success")
    return redirect(url_for("main.settings_page"))


@bp.post("/settings/reset")
def settings_reset():
    from ..scope import dataset_cache_reset
    conn = get_db()
    if request.form.get("confirm") != "DELETE":
        flash("Type DELETE to confirm - this removes every uploaded report.",
              "danger")
        return redirect(url_for("main.settings_page"))
    ingest.reset_all(conn, tenancy.current_upload_dir())
    dataset_cache_reset()
    flash("All data has been deleted.", "success")
    return redirect(url_for("main.upload_page"))


# --------------------------------------------------------------------------
# Alert preferences (Settings -> Alerts & notifications)
# --------------------------------------------------------------------------
# Persisted in the workspace's existing ``meta`` table via set_meta/get_meta -
# no new database, no schema change, no migration. Two boolean flags control
# alert assembly (see ``_apply_generation_flag``) and template display (see
# ``notif_flags`` on the request context).
_NOTIF_KEYS = {"in_app": "notif_in_app", "generation": "notif_generation"}


@bp.post("/settings/alerts")
def settings_alert_toggle():
    """Flip one of the two alert switches and bounce back to Settings."""
    which = (request.form.get("which") or "").strip()
    value = (request.form.get("value") or "").strip().lower()
    if which not in _NOTIF_KEYS:
        abort(400, "Unknown alert setting.")
    if value not in ("on", "off"):
        abort(400, "Alert switch expects 'on' or 'off'.")
    conn = get_db()
    database.set_meta(conn, _NOTIF_KEYS[which], "1" if value == "on" else "0")
    label = "In-app alerts" if which == "in_app" else "Alert generation"
    flash(f"{label} turned {'on' if value == 'on' else 'off'}.", "success")
    return redirect(url_for("main.settings_page") + "#alerts-notifications")


# --------------------------------------------------------------------------
# Profile & Account settings
# --------------------------------------------------------------------------
# Two thin pages reached from the top-right dropdown. They surface only the
# per-user fields the app actually stores (name, e-mail, password, avatar).
# Nothing here touches tenancy, hospital association, or roles: hospital name
# stays read-only because the app has no admin/staff authorization model to
# gate a rename safely, and letting any officer edit the tenant identity would
# break the ``hospitals.slug`` -> workspace-directory binding.
#
# Avatars live under the tenant's own directory tree
# (``instance/hospitals/<slug>/avatars/user_<uid>.<ext>``) so the existing
# per-hospital on-disk isolation keeps them compartmentalised without any
# schema change or shared upload pool. Filenames are derived from the
# session uid, never from the client's upload name.
_AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 MB on disk
_AVATAR_EXT_FOR_MIME = {"image/jpeg": "jpg", "image/png": "png",
                        "image/gif": "gif", "image/webp": "webp"}
_AVATAR_MIME_FOR_EXT = {v: k for k, v in _AVATAR_EXT_FOR_MIME.items()}
_AVATAR_SIGNATURES = {
    "jpg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),  # additionally requires ``WEBP`` at bytes 8..12
}


def _avatar_dir() -> Path:
    """The signed-in user's hospital-private avatars folder, created lazily."""
    slug = _signed_in()["hospital_slug"]
    d = tenancy.hospital_dir(slug) / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _avatar_current_path():
    """The on-disk file for the current user, or ``None`` if they have none."""
    uid = _signed_in()["id"]
    for p in _avatar_dir().glob(f"user_{uid}.*"):
        if p.suffix.lstrip(".").lower() in _AVATAR_SIGNATURES:
            return p
    return None


def _avatar_remove_current() -> None:
    uid = _signed_in()["id"]
    for p in _avatar_dir().glob(f"user_{uid}.*"):
        try:
            p.unlink()
        except OSError:
            pass


def _avatar_validate_and_read(fs):
    """Return ``(ext, bytes)`` on success or ``(None, error_message)``.

    Checks the declared MIME, the byte-level magic, and the size. The stored
    filename is always derived from the uid, never from ``fs.filename``, so a
    malicious upload name cannot escape the tenant directory.
    """
    if fs is None or not fs.filename:
        return None, "Choose an image file to upload."
    ext = _AVATAR_EXT_FOR_MIME.get(fs.mimetype)
    if not ext:
        return None, "Only JPG, PNG, GIF or WEBP images are accepted."
    data = fs.read()
    if not data:
        return None, "That file is empty."
    if len(data) > _AVATAR_MAX_BYTES:
        return None, (f"Avatar must be {int(_AVATAR_MAX_BYTES // (1024 * 1024))} "
                      f"MB or smaller.")
    if not any(data.startswith(sig) for sig in _AVATAR_SIGNATURES[ext]):
        return None, "The file's contents don't match an image of that type."
    if ext == "webp" and data[8:12] != b"WEBP":
        return None, "The WEBP header is malformed."
    return ext, data


@bp.get("/profile")
def profile_page():
    u = _signed_in()
    avatar = _avatar_current_path()
    return render_template(
        "profile.html", page="profile",
        has_avatar=avatar is not None,
        avatar_stamp=int(avatar.stat().st_mtime) if avatar else 0,
        initial=(((u["ico_officer"] or u["username"] or "U").strip()[:1]) or "U").upper(),
    )


@bp.post("/profile/update")
def profile_update():
    u = _signed_in()
    conn = get_accounts_db()
    name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    if len(name) > 120:
        flash("Name must be 120 characters or fewer.", "danger")
        return redirect(url_for("main.profile_page"))
    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Enter a valid e-mail address.", "danger")
        return redirect(url_for("main.profile_page"))
    if database.email_in_use_by_other(conn, email, u["id"]):
        flash("Another account is already using that e-mail.", "danger")
        return redirect(url_for("main.profile_page"))
    database.update_user_profile(conn, u["id"], ico_officer=name, email=email)
    flash("Profile updated.", "success")
    return redirect(url_for("main.profile_page"))


@bp.post("/profile/avatar")
def avatar_upload():
    ext, payload = _avatar_validate_and_read(request.files.get("avatar"))
    if ext is None:
        flash(payload, "danger")
        return redirect(url_for("main.profile_page"))
    uid = _signed_in()["id"]
    _avatar_remove_current()
    target = _avatar_dir() / f"user_{uid}.{ext}"
    try:
        target.write_bytes(payload)
    except OSError:
        flash("Couldn't save the image. Try again.", "danger")
        return redirect(url_for("main.profile_page"))
    flash("Profile photo updated.", "success")
    return redirect(url_for("main.profile_page"))


@bp.post("/profile/avatar/remove")
def avatar_remove():
    _avatar_remove_current()
    flash("Profile photo removed.", "success")
    return redirect(url_for("main.profile_page"))


@bp.get("/profile/avatar")
def avatar_image():
    p = _avatar_current_path()
    if not p:
        abort(404)
    ext = p.suffix.lstrip(".").lower()
    return send_from_directory(
        str(p.parent), p.name,
        mimetype=_AVATAR_MIME_FOR_EXT.get(ext, "application/octet-stream"),
        max_age=0, conditional=True)


@bp.get("/account")
def account_page():
    return render_template("account.html", page="account")


@bp.post("/account/password")
def account_password():
    u = _signed_in()
    conn = get_accounts_db()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    if not check_password_hash(u["password_hash"], current):
        flash("Your current password isn't correct.", "danger")
        return redirect(url_for("main.account_page"))
    if len(new) < 6:
        flash("Choose a new password of at least 6 characters.", "danger")
        return redirect(url_for("main.account_page"))
    if new != confirm:
        flash("The two new passwords don't match.", "danger")
        return redirect(url_for("main.account_page"))
    if new == current:
        flash("Pick a new password that differs from the current one.", "danger")
        return redirect(url_for("main.account_page"))
    database.set_password_hash(conn, u["id"], generate_password_hash(new))
    flash("Password changed.", "success")
    return redirect(url_for("main.account_page"))


@bp.post("/account/switch")
def account_switch():
    # The auth model here is single-identity per browser session:
    # ``auth.login_user`` clears the session before storing the new uid, and
    # there is no server-side session store to hold multiple identities.
    # A true parallel-account switcher would require a redesign out of scope
    # for this UI pass, so the honest minimal behaviour is: end this session
    # and hand the officer to the sign-in screen with a prompt. No cookies,
    # tokens or credentials are cached across the switch.
    _end_session()
    flash("Signed out. Enter the other account's details below.", "info")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------
# Small DB helpers, kept local so views stay readable
# --------------------------------------------------------------------------
def database_list(conn, limit: int = 50, offset: int = 0):
    return database.list_reports(conn, limit, offset)


def database_counts(conn) -> dict:
    return database.database_counts(conn)
