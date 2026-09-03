"""
Transmission-trend engine.

Finds the signal infection prevention cares about: the same organism showing up
in **multiple patients**, in **multiple wards**, in a **short time**.

Three complementary views are produced:

* :func:`pathogen_trends` - per-pathogen incidence, week-on-week movement and
  geographic spread;
* :func:`ward_matrix` - pathogen load per ward, so a single overloaded ward
  stands out;
* :func:`detect_clusters` - a rolling-window scan that reports every group of
  linked cases inside ``config.CLUSTER_WINDOW_DAYS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

import config
from ..pathogens import PATHOGENS
from .amr import classify_isolate
from .common import (Window, bucket_label, count_in,
                     episode_dedupe, observation_span,
                     windowed_excess_threshold,
                     patients_in, pathogen_display, pct, reference_date, safe_div,
                     wards_in, weekly_buckets, window_ending_on)


# --------------------------------------------------------------------------
# Per-pathogen trend summary
# --------------------------------------------------------------------------
@dataclass
class PathogenTrend:
    pathogen: str
    total: int = 0
    episodes: int = 0
    patients: int = 0
    wards: int = 0
    recent: int = 0
    recent_patients: int = 0
    recent_wards: int = 0
    baseline: int = 0
    baseline_patients: int = 0
    change_pct: float = 0.0
    weekly: list[int] = field(default_factory=list)
    weekly_labels: list[str] = field(default_factory=list)
    weekly_patients: list[int] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    top_ward: str = ""
    top_ward_count: int = 0
    ward_counts: list[tuple[str, int]] = field(default_factory=list)
    specimen_counts: list[tuple[str, int]] = field(default_factory=list)
    patients_per_week: float = 0.0
    mdr_rate: float = 0.0
    resistance_rate: float = 0.0
    markers: dict[str, int] = field(default_factory=dict)
    multi_ward: bool = False
    low_confidence: int = 0
    genus_only: int = 0

    @property
    def pathogen_display(self) -> str:
        return pathogen_display(self.pathogen)

    @property
    def direction(self) -> str:
        if self.baseline == 0 and self.recent == 0:
            return "flat"
        if self.baseline == 0 and self.recent > 0:
            return "emerging"
        if self.change_pct >= 25:
            return "rising"
        if self.change_pct <= -25:
            return "falling"
        return "flat"

    @property
    def full_name(self) -> str:
        p = PATHOGENS.get(self.pathogen)
        return p.name if p else self.pathogen

    @property
    def color(self) -> str:
        p = PATHOGENS.get(self.pathogen)
        return p.color if p else "#64748b"

    def as_dict(self) -> dict:
        """Flat JSON-safe form; properties are resolved so the UI stays dumb."""
        return {
            "pathogen": self.pathogen,
            "pathogen_display": pathogen_display(self.pathogen),
            "full_name": self.full_name,
            "color": self.color,
            "direction": self.direction,
            "total": self.total, "episodes": self.episodes,
            "patients": self.patients, "wards": self.wards,
            "recent": self.recent, "recent_patients": self.recent_patients,
            "recent_wards": self.recent_wards,
            "baseline": self.baseline, "baseline_patients": self.baseline_patients,
            "change_pct": self.change_pct,
            "weekly": self.weekly, "weekly_labels": self.weekly_labels,
            "weekly_patients": self.weekly_patients,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "top_ward": self.top_ward, "top_ward_count": self.top_ward_count,
            "ward_counts": [{"ward": w, "count": n} for w, n in self.ward_counts],
            "specimen_counts": [{"specimen": s, "count": n}
                                for s, n in self.specimen_counts],
            "patients_per_week": self.patients_per_week,
            "mdr_rate": self.mdr_rate, "resistance_rate": self.resistance_rate,
            "markers": self.markers, "multi_ward": self.multi_ward,
            "low_confidence": self.low_confidence, "genus_only": self.genus_only,
        }


def pathogen_trends(isolates, ref=None, n_buckets: int = 18) -> dict[str, PathogenTrend]:
    """One :class:`PathogenTrend` per tracked pathogen that has any data."""
    ref = ref or reference_date(isolates)
    recent_win = window_ending_on(ref, config.RECENT_WINDOW_DAYS)
    baseline_win = window_ending_on(ref, config.BASELINE_WINDOW_DAYS,
                                    offset_days=config.RECENT_WINDOW_DAYS)
    buckets = weekly_buckets(ref, n_buckets)

    grouped: dict[str, list] = {}
    for iso in isolates:
        if iso.pathogen:
            grouped.setdefault(iso.pathogen, []).append(iso)

    out: dict[str, PathogenTrend] = {}
    for pk in PATHOGENS:
        subset = grouped.get(pk, [])
        if not subset:
            continue
        t = PathogenTrend(pathogen=pk)
        t.total = len(subset)
        eps = episode_dedupe(subset)
        t.episodes = len(eps)
        t.patients = len({i.patient_id for i in subset})
        t.wards = len({i.ward for i in subset if i.ward})
        t.recent = count_in(subset, recent_win)
        t.recent_patients = len(patients_in(subset, recent_win))
        t.recent_wards = len(wards_in(subset, recent_win))
        t.baseline = count_in(subset, baseline_win)
        t.baseline_patients = len(patients_in(subset, baseline_win))
        t.change_pct = round(100.0 * safe_div(t.recent - t.baseline,
                                              max(1, t.baseline)), 1)
        t.weekly_labels = [bucket_label(w) for w in buckets]
        t.weekly = [count_in(subset, w) for w in buckets]
        t.weekly_patients = [len(patients_in(subset, w)) for w in buckets]
        dates = sorted(i.sample_date for i in subset if i.sample_date)
        t.first_seen, t.last_seen = (dates[0], dates[-1]) if dates else ("", "")
        span_days = 1
        if len(dates) > 1:
            span_days = max(7, (_d(dates[-1]) - _d(dates[0])).days + 1)
        t.patients_per_week = round(t.patients / max(1.0, span_days / 7.0), 2)

        ward_counts: dict[str, int] = {}
        for i in subset:
            ward_counts[i.ward] = ward_counts.get(i.ward, 0) + 1
        t.ward_counts = sorted(ward_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        t.top_ward, t.top_ward_count = t.ward_counts[0] if t.ward_counts else ("", 0)
        t.multi_ward = len([w for w, c in t.ward_counts if c >= 2]) >= 2

        sp: dict[str, int] = {}
        for i in subset:
            sp[i.specimen_type] = sp.get(i.specimen_type, 0) + 1
        t.specimen_counts = sorted(sp.items(), key=lambda kv: -kv[1])[:6]

        mdr = res = tested = 0
        for i in subset:
            cls = classify_isolate(i)
            if i.sensitivities:
                tested += 1
            if cls.is_mdr_or_worse:
                mdr += 1
            if cls.resistant_drugs:
                res += 1
        t.mdr_rate = pct(mdr, tested)
        t.resistance_rate = pct(res, tested)

        marker_counts: dict[str, int] = {}
        for i in subset:
            for m in i.markers:
                marker_counts[m] = marker_counts.get(m, 0) + 1
        t.markers = dict(sorted(marker_counts.items(), key=lambda kv: -kv[1]))

        t.low_confidence = sum(1 for i in subset if i.confidence < 0.9)
        t.genus_only = sum(1 for i in subset if i.genus_only)
        out[pk] = t
    return out


def _d(iso_date: str):
    from datetime import date
    try:
        return date.fromisoformat(iso_date)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Ward view
# --------------------------------------------------------------------------
def ward_matrix(isolates, ref=None, recent_days: int | None = None) -> list[dict]:
    """Pathogen load per ward with a per-ward recent/case rate."""
    recent_days = recent_days or config.RECENT_WINDOW_DAYS
    recent_win = window_ending_on(ref or reference_date(isolates), recent_days)
    grouped: dict[str, dict[str, int]] = {}
    totals: dict[str, dict] = {}
    for iso in isolates:
        if not iso.pathogen:
            continue
        # A blank / missing / whitespace-only ward is grouped into one shared
        # "Unspecified" bucket so those records are never silently dropped from
        # the ward view; a real ward label is kept exactly as recorded.
        ward = iso.ward if (iso.ward and iso.ward.strip()) else "Unspecified"
        w = totals.setdefault(ward, {
            "ward": ward, "isolates": 0, "patients": set(), "episodes": set(),
            "recent": 0, "recent_patients": set(), "mdr": 0, "tested": 0,
            "pathogens": {},
        })
        w["isolates"] += 1
        w["patients"].add(iso.patient_id)
        w["episodes"].add(iso.acquisition_key)
        w["pathogens"][iso.pathogen] = w["pathogens"].get(iso.pathogen, 0) + 1
        if recent_win.contains(iso.day):
            w["recent"] += 1
            w["recent_patients"].add(iso.patient_id)
        if iso.sensitivities:
            w["tested"] += 1
            if classify_isolate(iso).is_mdr_or_worse:
                w["mdr"] += 1

    rows = []
    for ward, w in totals.items():
        items = sorted(w["pathogens"].items(), key=lambda kv: -kv[1])
        rows.append({
            "ward": ward,
            "isolates": w["isolates"],
            "patients": len(w["patients"]),
            "episodes": len(w["episodes"]),
            "pathogen_count": len(items),
            "top_pathogen": pathogen_display(items[0][0]) if items else "",
            "top_share": pct(items[0][1], w["isolates"]) if items else 0,
            "recent": w["recent"],
            "recent_patients": len(w["recent_patients"]),
            "mdr_rate": pct(w["mdr"], w["tested"]),
            "high_consequence": _hc(ward),
            "breakdown": [{"pathogen": k, "display": pathogen_display(k),
                           "count": v, "color": PATHOGENS[k].color}
                          for k, v in items if k in PATHOGENS],
        })
    rows.sort(key=lambda r: (-r["isolates"], _ward_natural_key(r["ward"])))
    return rows


def _hc(ward: str) -> bool:
    from .common import is_high_consequence_ward
    return is_high_consequence_ward(ward)


def _ward_natural_key(label: str):
    """Tie-break key that sorts ward labels naturally.

    Ward labels of the form ``Ward 3`` / ``Ward 12`` compare against a plain
    string as ``"Ward 1" < "Ward 3"``, so Ward 12 would sort ahead of Ward 3
    when their isolate counts tie. Splitting on digit runs and casting them
    to ``int`` restores the numeric ordering humans expect; labels without any
    digits (``ICU``, ``Emergency Department``) reduce to a lowercased-string
    comparison, matching the previous behaviour for those rows.
    """
    parts = re.split(r"(\d+)", label or "")
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


# --------------------------------------------------------------------------
# Cluster detection (rolling window)
# --------------------------------------------------------------------------
@dataclass
class Cluster:
    pathogen: str
    start: str
    end: str
    span_days: int
    isolates: int
    patients: list[str]
    wards: list[str]
    specimens: list[str]
    mdr: int
    markers: list[str]
    average_confidence: float
    expected_patients: int = 0
    isolate_ids: list[int] = field(default_factory=list)

    @property
    def pathogen_display(self) -> str:
        return pathogen_display(self.pathogen)

    @property
    def n_patients(self) -> int:
        return len(self.patients)

    @property
    def n_wards(self) -> int:
        return len(self.wards)

    @property
    def ward_confined(self) -> bool:
        return len(self.wards) == 1

    @property
    def rate_per_week(self) -> float:
        return round(self.n_patients / max(1.0, self.span_days / 7.0), 2)

    @property
    def label(self) -> str:
        where = self.wards[0] if self.ward_confined else f"{self.n_wards} wards"
        return f"{pathogen_display(self.pathogen)} in {self.n_patients} patients, {where}"

    @property
    def excess(self) -> float:
        """How far above the endemic expectation this group sits."""
        return round(safe_div(self.n_patients, max(1, self.expected_patients)), 2)

    def as_dict(self) -> dict:
        return {
            "pathogen": self.pathogen,
            "pathogen_display": pathogen_display(self.pathogen),
            "start": self.start, "end": self.end, "span_days": self.span_days,
            "isolates": self.isolates, "patients": self.patients,
            "n_patients": self.n_patients, "wards": self.wards,
            "n_wards": self.n_wards, "specimens": self.specimens,
            "mdr": self.mdr, "markers": self.markers,
            "average_confidence": round(self.average_confidence, 2),
            "rate_per_week": self.rate_per_week,
            "ward_confined": self.ward_confined,
            "expected_patients": self.expected_patients,
            "excess": self.excess,
            "label": self.label,
            "isolate_ids": self.isolate_ids,
        }


def detect_clusters(isolates, ref=None, window_days: int | None = None,
                    min_patients: int | None = None, within=None) -> list[Cluster]:
    """Groups of same-pathogen cases that exceed what the hospital sees anyway.

    This is the direct answer to "is the same bacterium appearing in several
    patients or wards?" Clusters are emitted non-overlapping, strongest first.

    A bare "two isolates 12 days apart" is *not* a cluster: in a busy unit that
    is simply background noise, and reporting hundreds of them destroys the
    signal. A group is only emitted when it clears an expectation set by the
    organism's own endemic rate over a window of this length, **and** either

    * it is concentrated somewhere (>= 2 patients sharing one ward - a real
      transmission chain), or
    * it is large enough that its spread across wards is itself the finding.

    Pass ``within`` (a :class:`~bioguard.analysis.common.Window`) to restrict
    *where clusters may be found* while still learning the endemic rate from the
    full history - that is what the risk engine needs.
    """
    window_days = window_days or config.CLUSTER_WINDOW_DAYS
    floor = min_patients or config.CLUSTER_MIN_PATIENTS
    span = observation_span(isolates)
    grouped: dict[str, list] = {}
    for iso in isolates:
        if iso.pathogen and iso.day:
            grouped.setdefault(iso.pathogen, []).append(iso)

    clusters: list[Cluster] = []
    for pk, subset in grouped.items():
        # Learn the endemic background from this organism's whole history, then
        # judge each window against the episodes *outside* it - see
        # windowed_excess_threshold - so a dense first cluster is not asked to
        # beat a baseline it partly constitutes.
        episode_days = [x.day for x in episode_dedupe(subset)]
        if within is not None:
            subset = [x for x in subset if within.contains(x.day)]
            if not subset:
                continue
        subset = sorted(subset, key=lambda i: (i.day, i.id))
        i = 0
        while i < len(subset):
            anchor = subset[i].day
            members = [x for x in subset[i:]
                       if x.day <= anchor + timedelta(days=window_days - 1)]
            patients = {x.patient_id for x in members}
            need = windowed_excess_threshold(
                episode_days, anchor, span, window_days,
                config.CLUSTER_EXCESS_FACTOR, floor)
            if len(patients) >= need and _linked(members):
                c = _build_cluster(pk, members)
                c.expected_patients = need
                clusters.append(c)
                # Advance past this window so emitted clusters do not overlap.
                last = max(x.day for x in members)
                i = next((k for k, x in enumerate(subset) if x.day > last), len(subset))
                continue
            i += 1
    clusters.sort(key=lambda c: (-c.n_patients, -c.n_wards, c.start))
    return clusters


def _linked(members) -> bool:
    """Is there an epidemiological thread, or just co-incidence in a window?"""
    if len({m.patient_id for m in members}) >= config.CLUSTER_SPREAD_MIN_PATIENTS:
        return True
    per_ward: dict[str, set] = {}
    for m in members:
        per_ward.setdefault(m.ward, set()).add(m.patient_id)
    return any(len(ps) >= 2 for ps in per_ward.values())


def _build_cluster(pk: str, members: list) -> Cluster:
    days = [m.day for m in members if m.day]
    mdr = sum(1 for m in members if classify_isolate(m).is_mdr_or_worse)
    markers: dict[str, int] = {}
    for m in members:
        for tag in m.markers:
            markers[tag] = markers.get(tag, 0) + 1
    return Cluster(
        pathogen=pk,
        start=min(days).isoformat(),
        end=max(days).isoformat(),
        span_days=(max(days) - min(days)).days + 1,
        isolates=len(members),
        patients=sorted({m.patient_id for m in members}),
        wards=sorted({m.ward for m in members if m.ward}),
        specimens=sorted({m.specimen_type for m in members}),
        mdr=mdr,
        markers=sorted(markers, key=lambda k: -markers[k]),
        average_confidence=safe_div(sum(m.confidence for m in members), len(members)),
        isolate_ids=[m.id for m in members],
    )


def ward_clusters(isolates, ref=None, window_days: int | None = None,
                  within=None) -> list[Cluster]:
    """Clusters restricted to a single ward - the classic transmission event.

    The expectation is learned from that ward's own history for that organism,
    so a unit that always sees two cases a month is not flagged every month.
    ``within`` bounds where events may be found; see :func:`detect_clusters`.
    """
    window_days = window_days or config.CLUSTER_WINDOW_DAYS
    span = observation_span(isolates)
    grouped: dict[tuple[str, str], list] = {}
    for iso in isolates:
        if iso.pathogen and iso.day and iso.ward:
            grouped.setdefault((iso.pathogen, iso.ward), []).append(iso)
    out = []
    for (pk, ward), subset in grouped.items():
        episode_days = [x.day for x in episode_dedupe(subset)]
        if within is not None:
            subset = [x for x in subset if within.contains(x.day)]
            if not subset:
                continue
        subset = sorted(subset, key=lambda i: (i.day, i.id))
        i = 0
        while i < len(subset):
            anchor = subset[i].day
            members = [x for x in subset[i:]
                       if x.day <= anchor + timedelta(days=window_days - 1)]
            patients = {x.patient_id for x in members}
            need = windowed_excess_threshold(
                episode_days, anchor, span, window_days,
                config.CLUSTER_EXCESS_FACTOR, config.WARD_CLUSTER_MIN_PATIENTS)
            if len(patients) >= need:
                c = _build_cluster(pk, members)
                c.wards = [ward]
                c.expected_patients = need
                out.append(c)
                last = max(x.day for x in members)
                i = next((k for k, x in enumerate(subset) if x.day > last), len(subset))
                continue
            i += 1
    out.sort(key=lambda c: (-c.n_patients, c.start))
    return out


# --------------------------------------------------------------------------
# Time series for charts
# --------------------------------------------------------------------------
def timeline(isolates, ref=None, n_buckets: int = 18) -> dict:
    """Weekly isolate/patient counts, split by pathogen and by risk group."""
    ref = ref or reference_date(isolates)
    buckets = weekly_buckets(ref, n_buckets)
    labels = [bucket_label(w) for w in buckets]

    per_pathogen: dict[str, list[int]] = {}
    total_series, patient_series, mdr_series = [], [], []
    for idx, w in enumerate(buckets):
        in_w = [i for i in isolates if w.contains(i.day)]
        total_series.append(len(in_w))
        patient_series.append(len({i.patient_id for i in in_w}))
        mdr_series.append(sum(1 for i in in_w if classify_isolate(i).is_mdr_or_worse))
        for i in in_w:
            if i.pathogen:
                per_pathogen.setdefault(i.pathogen, [0] * len(buckets))[idx] += 1

    ordered = sorted(per_pathogen.items(), key=lambda kv: -sum(kv[1]))
    top = ordered[:8]
    top_keys = {pk for pk, _ in top}
    other = [sum(vals[i] for pk, vals in per_pathogen.items() if pk not in top_keys)
             for i in range(len(buckets))]
    series = [{"pathogen": pk, "display": pathogen_display(pk),
               "color": PATHOGENS[pk].color if pk in PATHOGENS else "#94a3b8",
               "data": vals} for pk, vals in top]
    if any(other):
        series.append({"pathogen": "other", "display": "Other tracked pathogens",
                       "color": "#94a3b8", "data": other})
    return {
        "labels": labels,
        "total": total_series,
        "patients": patient_series,
        "mdr": mdr_series,
        "series": series,
        "peak_week": labels[total_series.index(max(total_series))] if total_series else "",
        "peak_count": max(total_series) if total_series else 0,
    }


def recent_daily_trend(isolates, ref=None, days: int = 7) -> dict:
    """Cases per calendar day for the trailing ``days`` days ending at ``ref``.

    The single source of truth behind both the Alerts "last 7 days" panel and
    the Dashboard "Last 7 days" strip, so the two views can never drift apart.

    * anchored to the centralized :func:`reference_date` (never the wall clock),
      so a future-dated import cannot shift the window or appear inside it;
    * exactly ``days`` calendar buckets, inclusive of ``ref`` and the preceding
      ``days - 1`` days, oldest first - a zero-activity day is retained as 0;
    * an isolate lands in the bucket matching its sample day; rows with no
      valid sample date cannot be placed on a calendar day and are simply not
      counted here (they still belong to the dataset / KPI totals - Phase 2),
      and a date is never invented for them.

    ``count`` is the number of *items handed in*: callers pass the already
    episode-deduplicated set they want trended, so the metric reads as "new
    episodes per day". Returns a chart-friendly ``labels``/``series`` pair
    alongside the per-day ``points`` the bar markup renders.
    """
    ref = ref or reference_date(isolates)
    by_day: dict = {}
    for iso in isolates:
        d = iso.day
        if d is None:                    # missing / invalid date: not on any calendar day
            continue
        by_day[d] = by_day.get(d, 0) + 1
    dates = [ref - timedelta(days=off) for off in range(days - 1, -1, -1)]
    points = [{"iso": d.isoformat(), "weekday": f"{d:%a}", "date": f"{d:%d %b}",
               "count": by_day.get(d, 0)} for d in dates]
    series = [p["count"] for p in points]
    return {
        "days": days,
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "labels": [p["date"] for p in points],
        "series": series,
        "total": sum(series),
        "peak": max(series) if series else 0,
        "points": points,
    }


def monthly_incidence(isolates, ref=None, n_months: int = 6) -> dict:
    ref = ref or reference_date(isolates)
    from .common import monthly_windows
    wins = monthly_windows(ref, n_months)
    out = {"labels": [w.end.strftime("%b %Y") for w in wins], "series": {}}
    for iso in isolates:
        if not iso.pathogen:
            continue
        for i, w in enumerate(wins):
            if w.contains(iso.day):
                s = out["series"].setdefault(iso.pathogen, [0] * len(wins))
                s[i] += 1
                break
    return out


# --------------------------------------------------------------------------
# Patient-level view
# --------------------------------------------------------------------------
def patient_overview(isolates, ref=None, limit: int = 25) -> dict:
    """Patients carrying more than one target organism, or repeat positives."""
    by_patient: dict[str, list] = {}
    for iso in isolates:
        if iso.pathogen:
            by_patient.setdefault(iso.patient_id, []).append(iso)
    rows = []
    for pid, subset in by_patient.items():
        kinds = sorted({i.pathogen for i in subset})
        rows.append({
            "patient_id": pid,
            "isolates": len(subset),
            "pathogens": [pathogen_display(k) for k in kinds],
            "pathogen_keys": kinds,
            "distinct_pathogens": len(kinds),
            "wards": sorted({i.ward for i in subset if i.ward}),
            "first": min((i.sample_date for i in subset if i.sample_date), default=""),
            "last": max((i.sample_date for i in subset if i.sample_date), default=""),
            "mdr": sum(1 for i in subset if classify_isolate(i).is_mdr_or_worse),
            "high_consequence_ward": any(_hc(i.ward) for i in subset),
        })
    rows.sort(key=lambda r: (-r["distinct_pathogens"], -r["isolates"], r["last"]))
    multi = [r for r in rows if r["distinct_pathogens"] > 1]
    return {"total_patients": len(rows),
            "polymicrobial_patients": len(multi),
            "repeat_isolate_patients": sum(1 for r in rows if r["isolates"] > 1),
            "top": rows[:limit], "polymicrobial": multi[:limit]}


def other_organisms(isolates) -> list[dict]:
    """Everything detected but deliberately *not* tracked - full transparency."""
    counts: dict[str, dict] = {}
    for iso in isolates:
        if iso.pathogen:
            continue
        label = (iso.other_label or iso.organism_raw or "Unspecified").strip()
        key = label.lower()[:60]
        row = counts.setdefault(key, {"label": label, "raw": iso.organism_raw,
                                      "count": 0, "patients": set(),
                                      "method": iso.match_method})
        row["count"] += 1
        row["patients"].add(iso.patient_id)
    out = [{**v, "patients": len(v["patients"])} for v in counts.values()]
    out.sort(key=lambda d: -d["count"])
    return out[:40]
