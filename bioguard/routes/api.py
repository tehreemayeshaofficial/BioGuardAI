"""
JSON API.

Every dashboard widget is backed by one of these endpoints, so the same numbers
that render server-side can be re-fetched live after an upload without a page
reload - and the whole analysis layer is usable headlessly.

Read endpoints honour the session scope; pass ``?date_from=&date_to=&ward=&
pathogen=`` to override it for one call.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from flask import (Blueprint, Response, current_app, jsonify, request,
                   send_from_directory)

import config
from .. import database, get_db, ingest, tenancy
from ..analysis import amr, outbreak, trends
from ..pathogens import PATHOGENS, display_name
from ..scope import (current_scope, dataset as load_dataset,
                     dataset_cache_reset, resolve_scope)
from ..views import Insights

bp = Blueprint("api", __name__)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def view(target_only: bool = True) -> Insights:
    """Insights for the scope implied by this request."""
    conn = get_db()
    scope = current_scope()
    isolates = load_dataset(conn, scope, target_only=True)
    extra = isolates
    if scope.include_nontarget:
        extra = load_dataset(conn, scope, target_only=False)
    return Insights(conn=conn, scope=scope, isolates=isolates, all_rows=extra)


def fail(message: str, code: int = 400, **extra):
    """Error envelope: always JSON, always with an explicit ``ok`` flag."""
    body = {"ok": False, "error": message, "code": code}
    body.update(extra)
    return jsonify(body), code


def ok(payload: dict) -> Response:
    """Every successful response says so, and names its scope."""
    scope = current_scope()
    body = {"ok": True, "scope": {
        "date_from": scope.date_from, "date_to": scope.date_to,
        "ward": scope.ward, "pathogen": scope.pathogen,
        "pathogen_display": scope.pathogen_display,
        "include_nontarget": scope.include_nontarget,
        "label": scope.label, "filtered": scope.is_filtered,
    }}
    body.update(payload)
    return jsonify(body)


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------
@bp.get("/api/health")
def health():
    conn = get_db()
    counts = database.database_counts(conn)
    return ok({"status": "ok", "database": counts,
               "reference_date": str(counts.get("last_date") or ""),
               "upload_dir_writable": _writable(),
               "version": _version(),
               "thresholds": {
                   "recent_window_days": config.RECENT_WINDOW_DAYS,
                   "risk_high": config.RISK_HIGH_THRESHOLD,
                   "risk_medium": config.RISK_MEDIUM_THRESHOLD,
               }})


def _writable() -> bool:
    from pathlib import Path
    p = Path(tenancy.current_upload_dir())
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _version() -> str:
    import bioguard
    return bioguard.__version__


@bp.get("/api/scope")
def get_scope():
    scope = current_scope()
    conn = get_db()
    return ok({"scope": {
        "date_from": scope.date_from, "date_to": scope.date_to,
        "ward": scope.ward, "pathogen": scope.pathogen,
        "include_nontarget": scope.include_nontarget,
        "label": scope.label, "chips": scope.chips(),
    }, "options": {
        "wards": database.available_wards(conn),
        "pathogens": [k for k in database.available_pathogens(conn)
                      if k in PATHOGENS],
    }})


@bp.route("/api/scope", methods=["POST", "DELETE"])
def set_scope():
    """POST a JSON body to set the scope; DELETE to clear it."""
    if request.method == "DELETE":
        resolve_scope({"clear": "1"})
        return ok({"cleared": True})
    body = request.get_json(silent=True) or dict(request.values)
    allowed = {"date_from", "date_to", "ward", "pathogen", "include_nontarget"}
    clean = {k: v for k, v in body.items() if k in allowed}
    scope = resolve_scope(clean)
    return ok({"applied": {
        "date_from": scope.date_from, "date_to": scope.date_to,
        "ward": scope.ward, "pathogen": scope.pathogen,
        "include_nontarget": scope.include_nontarget, "label": scope.label,
    }})


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
@bp.post("/api/upload")
def upload():
    conn = get_db()
    files = request.files.getlist("file") + request.files.getlist("files") \
        + request.files.getlist("files[]")
    files = [f for f in files if f and f.filename]
    if not files:
        return fail("No file received. Send a multipart form with field 'file'.",
                    400, allowed=sorted(config.ALLOWED_EXTENSIONS))
    allow_dup = request.values.get("allow_duplicate") in ("1", "true", "yes")

    results = []
    for fs in files:
        res = ingest.ingest_upload(
            conn, fs, upload_dir=tenancy.current_upload_dir(),
            allow_duplicate=allow_dup)
        dataset_cache_reset()
        results.append(res.as_dict())
    stored = sum(r["isolates"] for r in results)
    dataset_cache_reset()
    return ok({"results": results, "files": len(results),
               "isolates_stored": stored,
               "imported": sum(1 for r in results if r["status"] == "imported"),
               "message": f"{stored:,} new isolate(s) stored from "
                          f"{len(results)} file(s)."})


@bp.get("/api/samples")
def samples():
    from ..routes.main import SAMPLE_DIR, available_samples
    if not SAMPLE_DIR.exists():
        return ok({"samples": []})
    return ok({"samples": available_samples()})


@bp.get("/api/samples/<name>")
def sample_download(name: str):
    from ..routes.main import SAMPLE_DIR
    safe = Path(name).name
    if not safe.endswith((".csv", ".tsv", ".txt", ".pdf")):
        return fail("Unsupported sample type.", 400)
    if not (SAMPLE_DIR / safe).exists():
        return fail(f"No sample named '{safe}'.", 404)
    return send_from_directory(SAMPLE_DIR, safe, as_attachment=True)


# --------------------------------------------------------------------------
# Analysis roll-ups
# --------------------------------------------------------------------------
@bp.get("/api/summary")
def summary():
    v = view()
    k = v.kpis
    return ok({
        "kpis": k,
        "status": v.status_line,
        "risk_counts": v.outbreak_summary["counts"],
        "overall_level": k["overall_level"],
        "overall_score": k["overall_score"],
        "high": v.outbreak_summary["high"],
        "medium": v.outbreak_summary["medium"],
        "findings": v.findings[:10],
        "alerts": v.amr_summary["alerts"],
        "clusters": v.cluster_rows[:10],
        "ward_clusters": v.ward_cluster_rows[:10],
        "mdr_rate": k["mdr_rate"],
        "as_of": k["as_of"],
    })


@bp.get("/api/outbreak")
def outbreak_api():
    v = view()
    summ = v.outbreak_summary
    return ok({
        "as_of": summ["as_of"], "window": summ["window"],
        "overall_level": summ["overall_level"],
        "overall_score": summ["overall_score"],
        "counts": summ["counts"],
        "ranked": summ["ranked"],
        "high": summ["high"], "medium": summ["medium"], "low": summ["low"],
        "clusters": v.cluster_rows,
        "ward_clusters": v.ward_cluster_rows,
        "ward_assessments": v.risk_units,
        "high_consequence_wards": summ["high_consequence_wards"],
        "recent_isolates": summ["recent_isolates"],
        "recent_patients": summ["recent_patients"],
        "model": {"weights": config.RISK_WEIGHTS,
                  "thresholds": {"High": config.RISK_HIGH_THRESHOLD,
                                 "Medium": config.RISK_MEDIUM_THRESHOLD},
                  "saturation": config.RISK_SATURATION,
                  "endemic_gate_min": config.RISK_ENDEMIC_GATE_MIN},
    })


@bp.get("/api/amr")
def amr_api():
    v = view()
    summ = v.amr_summary
    return ok({
        "as_of": summ["as_of"],
        "overall_mdr_rate": summ["overall_mdr_rate"],
        "per_pathogen": summ["per_pathogen"],
        "ranked": summ["ranked"],
        "gram_negative": summ["gram_negative"],
        "gram_positive": summ["gram_positive"],
        "alerts": summ["alerts"],
        "highest_resistance": summ["highest_resistance"],
        "last_line_exhausted": summ["last_line_exhausted"],
        "movements": v.amr_trends["movements"],
        "monthly": v.amr_trends,
        "rising": [m for m in v.amr_trends["movements"]
                   if m["direction"] == "rising"
                   and m["tests_recent"] >= config.MIN_ISOLATES_FOR_RESISTANCE_RATE],
    })


@bp.get("/api/trends")
def trends_api():
    v = view()
    return ok({
        "as_of": v.ref.isoformat(),
        "pathogens": v.trend_rows,
        "rising": [r for r in v.trend_rows if r["direction"] in ("rising", "emerging")],
        "multi_patient": [r for r in v.trend_rows if r["recent_patients"] >= 2],
        "multi_ward": [r for r in v.trend_rows if r["recent_wards"] >= 2],
        "timeline": v.timeline,
        "wards": v.ward_rows,
        "patients": v.patient_rows["total_patients"],
        "polymicrobial": v.patient_rows["polymicrobial_patients"],
    })


@bp.get("/api/timeline")
def timeline_api():
    v = view()
    n = int(request.args.get("buckets", 18))
    return ok({"timeline": trends.timeline(v.isolates, v.ref,
                                           max(4, min(60, n))),
               "as_of": v.ref.isoformat()})


@bp.get("/api/clusters")
def clusters_api():
    v = view()
    return ok({
        "as_of": v.ref.isoformat(),
        "window_days": config.CLUSTER_WINDOW_DAYS,
        "clusters": v.cluster_rows,
        "ward_clusters": v.ward_cluster_rows,
        "total": len(v.cluster_rows),
        "policy": {"min_patients": config.CLUSTER_MIN_PATIENTS,
                   "excess_factor": config.CLUSTER_EXCESS_FACTOR,
                   "spread_min_patients": config.CLUSTER_SPREAD_MIN_PATIENTS},
    })


@bp.get("/api/wards")
def wards_api():
    v = view()
    return ok({"wards": v.ward_rows, "risk_units": v.risk_units,
               "ward_clusters": v.ward_cluster_rows,
               "protected": sorted(config.HIGH_CONSEQUENCE_WARDS)})


@bp.get("/api/patients")
def patients_api():
    v = view()
    return ok(v.patient_rows)


@bp.get("/api/findings")
def findings_api():
    v = view()
    limit = int(request.args.get("limit", 20))
    return ok({"findings": v.findings[:max(1, min(100, limit))],
               "status": v.status_line})


@bp.get("/api/pathogens")
def pathogens_api():
    v = view()
    trends_by_pk = v.pathogen_trends
    risk_by_pk = {a["pathogen"]: a for a in v.outbreak_summary["ranked"]}
    amr_by_pk = v.amr_summary["per_pathogen"]
    rows = []
    for pk, meta in PATHOGENS.items():
        t = trends_by_pk.get(pk)
        rows.append({
            "key": pk, "name": meta.name, "short_name": meta.short_name,
            "gram": meta.gram, "group": meta.group, "color": meta.color,
            "mdro": meta.mdro, "public_health": meta.public_health,
            "phenotype": meta.phenotype, "threat": meta.threat,
            "present": bool(t),
            "trend": t.as_dict() if t else None,
            "risk": risk_by_pk.get(pk),
            "amr": amr_by_pk.get(pk),
        })
    rows.sort(key=lambda r: (-(r["risk"]["score"] if r["risk"] else 0)))
    return ok({"pathogens": rows, "count": len(rows),
               "tracked": len(PATHOGENS),
               "seen": sum(1 for r in rows if r["present"])})


@bp.get("/api/pathogen/<key>")
def pathogen_api(key: str):
    v = view()
    detail = v.pathogen_detail(key)
    if detail is None:
        return fail(f"'{key}' is not one of the tracked pathogens.", 404,
                    known=sorted(PATHOGENS))
    detail = dict(detail)
    detail["isolates"] = [{
        "id": i.id, "patient_id": i.patient_id, "ward": i.ward,
        "date": i.sample_date, "specimen": i.specimen_type,
        "organism_raw": i.organism_raw, "report_id": i.report_id,
        "class": amr.classify_isolate(i).label or "Susceptible",
        "resistant": [display_name(d) for d in i.resistant_drugs],
        "markers": i.markers,
    } for i in detail["isolates"][:200]]
    return ok(detail)


# --------------------------------------------------------------------------
# Raw data
# --------------------------------------------------------------------------
@bp.get("/api/isolates")
def isolates_api():
    v = view()
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    return ok(v.isolate_rows(limit=max(1, min(1000, limit)),
                             offset=max(0, offset),
                             search=request.args.get("q", "")))


@bp.get("/api/export/isolates.csv")
def export_isolates():
    """Download the scoped dataset as a flat CSV, antibiogram included."""
    v = view()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["patient_id", "sample_date", "ward", "specimen_type",
                     "organism_reported", "pathogen", "matched_by", "confidence",
                     "markers", "resistance_class", "agents_tested",
                     "agents_resistant", "report_id"])
    for iso in v.isolate_rows(limit=10 ** 6)["rows"]:
        writer.writerow([iso["patient_id"], iso["date"], iso["ward"],
                         iso["specimen"], iso["organism_raw"],
                         iso["pathogen_display"], iso["method"], iso["confidence"],
                         ";".join(iso["markers"]), iso["class"], iso["n_tested"],
                         ";".join(iso["resistant"]), iso["report_id"]])
    resp = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
    scope = current_scope()
    stamp = f"{scope.pathogen or 'all'}-{scope.ward or 'all-wards'}-{v.ref.isoformat()}"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in stamp)
    resp.headers["Content-Disposition"] = f'attachment; filename="bioguard-{safe}.csv"'
    return resp


# --------------------------------------------------------------------------
# Reports + data management
# --------------------------------------------------------------------------
@bp.get("/api/reports")
def reports_api():
    conn = get_db()
    rows = database.list_reports(
        conn, max(1, min(200, int(request.args.get("limit", 50)))),
        max(0, int(request.args.get("offset", 0))))
    return ok({"reports": [dict(r) for r in rows],
               "counts": database.database_counts(conn)})


@bp.delete("/api/reports/<int:report_id>")
def delete_report(report_id: int):
    conn = get_db()
    if not database.delete_report(conn, report_id):
        return fail(f"There is no report #{report_id}.", 404)
    dataset_cache_reset()
    return ok({"deleted": report_id,
               "counts": database.database_counts(conn)})


@bp.post("/api/demo/seed")
def seed_demo():
    conn = get_db()
    force = request.values.get("force") in ("1", "true", "yes")
    res = ingest.seed_demo(conn, force=force)
    dataset_cache_reset()
    return jsonify({"ok": res["status"] in ("seeded", "already"), **res,
                    "counts": database.database_counts(conn)})


@bp.delete("/api/demo")
def remove_demo():
    conn = get_db()
    n = ingest.wipe_demo(conn)
    dataset_cache_reset()
    return ok({"removed_sensitivities": n,
               "counts": database.database_counts(conn)})
