"""
Page-level view model.

The analysis engines return objects; templates and JSON endpoints want one
plain dictionary they can walk without importing Python classes. :class:`Insights`
is that seam: it computes every roll-up a page might need, lazily, over a single
loaded dataset, so a dashboard render and its matching ``/api`` call describe
exactly the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import config
from . import database
from .analysis import amr, outbreak, trends
from .analysis.common import (count_in, distinct_events, episode_dedupe,
                              patients_in, reference_date,
                              variant_sensitivity_events, window_ending_on,
                              wards_in)
from .pathogens import PATHOGENS, display_name


@dataclass
class Insights:
    """Every derived view over one scoped dataset, computed on demand.

    ``isolates`` is the tracked set the analytics run over. ``all_rows`` holds
    the same scope *without* the target-organism filter, so the raw-data browser
    can show what was deliberately not counted.
    """
    conn: object = None
    scope: object = None
    isolates: list = None
    all_rows: list = None

    def __post_init__(self):
        self.isolates = self.isolates or []

    @property
    def everything(self) -> list:
        return self.all_rows if self.all_rows is not None else self.isolates

    # ---- basics ----------------------------------------------------------
    @cached_property
    def ref(self):
        return reference_date(self.isolates)

    @cached_property
    def recent_window(self):
        return window_ending_on(self.ref, config.RECENT_WINDOW_DAYS)

    @cached_property
    def prior_window(self):
        return window_ending_on(self.ref, config.RECENT_WINDOW_DAYS,
                                offset_days=config.RECENT_WINDOW_DAYS)

    @cached_property
    def episodes(self):
        """One isolate per (patient, organism) - repeat cultures are not new cases."""
        return episode_dedupe(self.isolates)

    @cached_property
    def undated_isolates(self) -> int:
        """Isolates with no parseable sample date (missing or invalid).

        They stay in every volume/KPI count but are excluded from all
        time-based views (windows, timeline, outbreak); the dashboard
        discloses how many are held back from those charts.
        """
        return sum(1 for i in self.isolates if i.day is None)

    @cached_property
    def event_isolates(self) -> list:
        """Cross-report duplicate copies collapsed to one row per lab event.

        The isolate / sensitivity / ward *volume* counts are taken from this, so
        an identical export imported under two overlapping reports is counted
        once. Episode, patient and pathogen roll-ups are already duplicate-safe.
        The raw rows stay in ``self.isolates`` for the audit trail and the
        per-report browser; classification/scoring engines keep their own inputs
        and are handled in their own phases.
        """
        return distinct_events(self.isolates)

    @cached_property
    def variant_sensitivity_events(self) -> int:
        """Events whose duplicate copies disagree on susceptibility (flagged, not resolved)."""
        return variant_sensitivity_events(self.isolates)

    @cached_property
    def scoped(self) -> bool:
        return bool(self.scope and self.scope.is_filtered)

    # ---- engines ---------------------------------------------------------
    @cached_property
    def outbreak_summary(self) -> dict:
        return outbreak.summary(self.isolates, self.ref)

    @cached_property
    def amr_summary(self) -> dict:
        # AMR consumes the de-duplicated event view so a cross-report duplicate
        # cannot inflate the antibiogram, the isolate-level burden counts or the
        # last-line list. Episode-denominated rates were already duplicate-safe
        # (worst class per patient+organism); raw rows stay in self.isolates.
        return amr.summary(self.event_isolates, self.ref)

    @cached_property
    def amr_trends(self) -> dict:
        return amr.trends(self.event_isolates, self.ref)

    @cached_property
    def pathogen_trends(self) -> dict:
        return trends.pathogen_trends(self.isolates, self.ref)

    @cached_property
    def trend_rows(self) -> list[dict]:
        rows = [t.as_dict() for t in self.pathogen_trends.values()]
        rows.sort(key=lambda r: (-r["recent"], -r["total"]))
        return rows

    @cached_property
    def timeline(self) -> dict:
        return trends.timeline(self.isolates, self.ref)

    @cached_property
    def recent_daily_trend(self) -> dict:
        """Compact trailing-7-day daily case trend (new episodes/day) for the
        dashboard - the same centralized builder the Alerts panel uses, so the
        two views cannot drift apart."""
        return trends.recent_daily_trend(self.episodes, self.ref)

    @cached_property
    def ward_rows(self) -> list[dict]:
        return trends.ward_matrix(self.event_isolates, self.ref)

    @cached_property
    def cluster_rows(self) -> list[dict]:
        return [c.as_dict() for c in trends.detect_clusters(self.isolates, self.ref)]

    @cached_property
    def ward_cluster_rows(self) -> list[dict]:
        return [c.as_dict() for c in trends.ward_clusters(self.isolates, self.ref)]

    @cached_property
    def recent_ward_clusters(self) -> list[dict]:
        """Ward clusters newest first - what the dashboard should be showing."""
        return sorted(self.ward_cluster_rows,
                      key=lambda c: (c["end"], c["n_patients"]), reverse=True)

    @cached_property
    def patient_rows(self) -> dict:
        return trends.patient_overview(self.isolates, self.ref)

    @cached_property
    def risk_units(self) -> list[dict]:
        return self.outbreak_summary["ward_assessments"]

    # ---- headline numbers ------------------------------------------------
    @cached_property
    def kpis(self) -> dict:
        events = self.event_isolates
        recent = count_in(events, self.recent_window)
        prior = count_in(events, self.prior_window)
        recent_eps = count_in(self.episodes, self.recent_window)
        prior_eps = count_in(self.episodes, self.prior_window)
        dates = sorted(i.sample_date for i in self.isolates if i.sample_date)
        counts = self.scope_db_counts()
        return {
            "isolates": len(events),
            "raw_rows": len(self.isolates),
            "duplicate_rows": len(self.isolates) - len(events),
            "variant_sensitivity_events": self.variant_sensitivity_events,
            "episodes": len(self.episodes),
            "patients": len({i.patient_id for i in self.isolates}),
            "wards": len({i.ward for i in self.isolates if i.ward}),
            "pathogens_seen": len({i.pathogen for i in self.isolates if i.pathogen}),
            "reports": counts.get("reports", 0),
            "sensitivities": sum(len(i.sensitivities) for i in events),
            "recent_isolates": recent,
            "recent_patients": len(patients_in(self.isolates, self.recent_window)),
            "recent_wards": len(wards_in(self.isolates, self.recent_window)),
            "recent_episodes": recent_eps,
            "prior_episodes": prior_eps,
            "change_pct": round(100.0 * (recent_eps - prior_eps) / prior_eps, 1)
            if prior_eps else None,
            "volume_change_pct": round(100.0 * (recent - prior) / prior, 1)
            if prior else None,
            "high": self.outbreak_summary["counts"]["High"],
            "medium": self.outbreak_summary["counts"]["Medium"],
            "low": self.outbreak_summary["counts"]["Low"],
            "overall_level": self.outbreak_summary["overall_level"],
            "overall_score": self.outbreak_summary["overall_score"],
            "clusters": len(self.cluster_rows),
            "ward_clusters": len(self.ward_cluster_rows),
            "mdr_rate": self.amr_summary["overall_mdr_rate"],
            "xdr_pdr": sum(v["by_episode"]["xdr_or_pdr"]
                           for v in self.amr_summary["per_pathogen"].values()),
            "first_date": dates[0] if dates else "",
            "last_date": dates[-1] if dates else "",
            "undated_isolates": self.undated_isolates,
            "as_of": self.ref.isoformat(),
            "window": str(self.recent_window),
            "window_days": config.RECENT_WINDOW_DAYS,
        }

    def scope_db_counts(self) -> dict:
        """Whole-database counts, for context beside the scoped ones."""
        if self.conn is None:
            return {}
        return database.database_counts(self.conn)

    # ---- the twelve-target grid ------------------------------------------
    @cached_property
    def detection_grid(self) -> list[dict]:
        """One card per tracked organism, *including the ones not found*.

        Showing the negatives is the point: a surveillance dashboard that only
        lists what it detected cannot answer "did we look for everything?"
        """
        risk = {a["pathogen"]: a for a in self.outbreak_summary["ranked"]}
        amr_rows = self.amr_summary["per_pathogen"]
        rows = []
        for pk, meta in PATHOGENS.items():
            t = self.pathogen_trends.get(pk)
            a = risk.get(pk)
            b = amr_rows.get(pk)
            rows.append({
                "key": pk, "name": meta.name, "short_name": meta.short_name,
                "gram": meta.gram, "group": meta.group, "color": meta.color,
                "mdro": meta.mdro, "public_health": meta.public_health,
                "phenotype": meta.phenotype, "threat": meta.threat,
                "detected": bool(t),
                "total": t.total if t else 0,
                "patients": t.patients if t else 0,
                "wards": t.wards if t else 0,
                "recent": t.recent if t else 0,
                "recent_patients": t.recent_patients if t else 0,
                "change_pct": t.change_pct if t else None,
                "direction": t.direction if t else "absent",
                "mdr_rate": (b["mdr_rate"] if b else 0),
                "xdr": (b["xdr"] + b["pdr"]) if b else 0,
                "level": a["level"] if a else "None",
                "score": a["score"] if a else 0,
                "excess_ratio": a["excess_ratio"] if a else 0,
                "expected_cases": a["expected_cases"] if a else 0,
                "clusters": len(a["clusters"]) if a else 0,
                "top_ward": t.top_ward if t else "",
                "markers": list(t.markers.items())[:3] if t else [],
                "weekly": t.weekly if t else [],
                "last_seen": t.last_seen if t else "",
            })
        rows.sort(key=lambda r: (-r["score"], -r["recent"], -r["total"],
                                 r["short_name"]))
        return rows

    @cached_property
    def undetected(self) -> list[dict]:
        return [r for r in self.detection_grid if not r["detected"]]

    # ---- the "act today" list --------------------------------------------
    @cached_property
    def findings(self) -> list[dict]:
        """A single prioritised queue: what an infection-prevention team opens first.

        Outbreak escalations outrank raw case counts, because a High band is only
        reached after the organism's own endemic expectation was beaten.
        """
        out: list[dict] = []
        for a in self.outbreak_summary["ranked"]:
            if a["level"] == "Low" and not a["escalation_reasons"]:
                continue
            priority = {"High": 100, "Medium": 60, "Low": 25}[a["level"]]
            priority += min(20, a["score"] / 5)
            if a["escalation_reasons"]:
                priority += 12
            out.append({
                "priority": round(priority, 1),
                "kind": "outbreak",
                "level": a["level"],
                "badge": f"{a['level']} risk - {a['score']}",
                "title": a["pathogen_display"],
                "detail": (a["escalation_reasons"][0] if a["escalation_reasons"]
                           else a["components"][0]["detail"] if a["components"] else ""),
                "meta": (f"{a['recent_cases']} cases in {a['recent_patients']} patients"
                         f" / {a['recent_wards']} wards over "
                         f"{config.RECENT_WINDOW_DAYS} days"),
                "action": a["recommended_action"],
                "action_items": a.get("actions", []),
                "pathogen": a["pathogen"],
                "link": f"/pathogen/{a['pathogen']}",
            })

        for c in self.recent_ward_clusters[:6]:
            if c["n_patients"] < config.WARD_CLUSTER_MIN_PATIENTS:
                continue
            protected = _hc(c["wards"][0]) if c["wards"] else False
            out.append({
                "priority": 70 + c["n_patients"] * 3 + (15 if protected else 0),
                "kind": "cluster",
                "level": "High" if (protected and c["n_patients"] >= 3)
                else ("Medium" if c["n_patients"] >= 4 else "Low"),
                "badge": f"{c['n_patients']} patients, one ward",
                "title": f"{c['pathogen_display']} on {c['wards'][0]}"
                if c["wards"] else c["pathogen_display"],
                "detail": (f"{c['n_patients']} patients in {c['span_days']} days"
                           + (f" ({c['mdr']} MDR or worse)" if c["mdr"] else "")
                           + (" - protected cohort" if protected else "")),
                "meta": f"{c['start']} to {c['end']}",
                "action": "Review bed allocation, staffing and shared equipment on "
                          "the ward; enhance cleaning and initiate contact screening.",
                "pathogen": c["pathogen"],
                "ward": c["wards"][0] if c["wards"] else "",
                "link": f"/pathogen/{c['pathogen']}",
            })

        for al in self.amr_summary["alerts"][:5]:
            if al["severity"] not in ("Critical", "High"):
                continue
            out.append({
                "priority": 55 + al["rate"] / 2 + (15 if al["is_critical"] else 0),
                "kind": "amr",
                "level": "High" if al["severity"] == "Critical" else "Medium",
                "badge": f"{al['rate']}% resistant",
                "title": f"{al['pathogen_display']} - {al['antibiotic_display']}",
                "detail": (f"{al['resistant']}/{al['tested']} tested isolates resistant"
                           + ("; this is a last-line agent" if al["is_critical"] else "")),
                "meta": f"{al['category']}",
                "action": "Review empiric therapy guidance and the antibiogram period; "
                          "consider contact precautions for colonised patients.",
                "pathogen": al["pathogen"],
                "link": f"/amr",
            })

        exhausted = self.amr_summary["last_line_exhausted"]
        if exhausted:
            pk = exhausted[0]["pathogen"]
            out.append({
                "priority": 95,
                "kind": "amr",
                "level": "High",
                "badge": f"{len(exhausted)} isolate(s), no last-line option",
                "title": "Last-resort agents exhausted",
                "detail": f"{exhausted[0]['pathogen_display']} in patient "
                          f"{exhausted[0]['patient_id']} ({exhausted[0]['ward']}) is "
                          f"non-susceptible to every last-line agent tested: "
                          f"{', '.join(exhausted[0]['agents'])}.",
                "meta": exhausted[0]["date"],
                "action": "Isolate the patient, involve microbiology and infectious "
                          "diseases immediately, and screen for epidemiological links.",
                "pathogen": pk,
                "link": f"/pathogen/{pk}",
            })

        out.sort(key=lambda d: -d["priority"])
        return out

    @cached_property
    def status_line(self) -> dict:
        """One sentence that tells a busy reader whether to worry."""
        level = self.kpis["overall_level"]
        n = self.kpis["high"]
        if not self.isolates:
            return {"level": "None", "tone": "muted",
                    "text": "No tracked organisms in this scope yet - upload a lab "
                            "report or load the demonstration dataset."}
        if n:
            names = ", ".join(a["pathogen_display"]
                              for a in self.outbreak_summary["high"][:3])
            more = f" (+{n - 3} more)" if n > 3 else ""
            text = (f"{n} pathogen{'' if n == 1 else 's'} at HIGH outbreak risk: "
                    f"{names}{more}.")
        elif self.kpis["medium"]:
            text = (f"{self.kpis['medium']} pathogen(s) at elevated activity; no "
                    f"outbreak-level signal above endemic expectation.")
        else:
            text = "All tracked pathogens are within their endemic expectation."
        return {"level": level, "tone": level.lower(), "text": text}

    @cached_property
    def others(self) -> list[dict]:
        """Organisms seen but not tracked - full transparency about exclusions."""
        return trends.other_organisms(self.everything)

    # ---- the alerts page -------------------------------------------------
    @cached_property
    def alert_center(self) -> dict:
        """The single most actionable outbreak alert, packaged for the Alerts page.

        Everything the alert card, the seven-day trend, the resistance table and
        the pre-filled notification links need is assembled here, so the template
        stays presentation-only and never recomputes a statistic.
        """
        from urllib.parse import urlencode

        ranked = self.outbreak_summary["ranked"]
        high = [a for a in ranked if a["level"] == "High"]
        active = bool(high)
        a = high[0] if high else (ranked[0] if ranked else None)
        if a is None:
            return {"active": False, "trend": [], "agents": [],
                    "counts": self.outbreak_summary["counts"],
                    "subject": "", "body": "", "mailto": "", "whatsapp": ""}

        pk = a["pathogen"]
        subset = [i for i in self.isolates if i.pathogen == pk]
        wards_seen = {i.ward for i in subset if i.ward and i.ward != "Unspecified"}

        # Hottest ward is where an intervention actually happens.
        ward = a["hot_wards"][0]["ward"] if a["hot_wards"] else ""
        if not ward:
            ward = next(iter(sorted(wards_seen)), "")
        other_wards = max(0, len(wards_seen) - (1 if ward else 0))

        # Daily case trend for the seven days ending at the reference date,
        # counted as acquisitions (episode-deduplicated), not repeat cultures.
        eps = [i for i in self.episodes if i.pathogen == pk]
        # Same centralized builder behind the dashboard "Last 7 days" strip, so
        # the two views cannot drift. Numerically identical to the previous
        # inline loop: 7 reference-anchored daily buckets over this organism's
        # episodes (acquisitions, not repeat cultures).
        day_trend = trends.recent_daily_trend(eps, self.ref)
        trend, peak, total7 = day_trend["points"], day_trend["peak"], day_trend["total"]

        # Antibiogram with a plain Resistant / Intermediate / Sensitive verdict.
        agents = amr.agent_rows(subset, min_tests=1)[:12]
        for r in agents:
            rate = r["rate"]
            r["status"] = ("Resistant" if rate >= 50 else
                           "Intermediate" if rate >= 20 else "Sensitive")

        reasons = a["escalation_reasons"] or []
        action = a["recommended_action"]
        first_action = action.split(" \u2022 ")[0]
        spread = (f" on {ward}" if ward else "") + \
                 (f" and {other_wards} other ward{'s' if other_wards != 1 else ''}"
                  if other_wards else "")
        recommendation = (
            f"{a['full_name']} is active in {a['recent_patients']} patient"
            f"{'s' if a['recent_patients'] != 1 else ''}{spread}. "
            f"{a['recent_cases']} recent case{'s' if a['recent_cases'] != 1 else ''} "
            f"against an endemic expectation of {a['expected_cases']} "
            f"({a['excess_ratio']}\u00d7). "
            + (f"{reasons[0]}. " if reasons else "")
            + (f"Resistance burden is {a['mdr_rate']}% MDR-or-worse. "
               if a.get("mdr_rate") else "")
            + f"Priority action: {first_action}.")

        lines = [
            "BIOGUARD AI OUTBREAK ALERT - " + a["level"].upper() + " RISK",
            f"Organism: {a['full_name']} ({a['pathogen_display']})",
            f"Ward: {ward or 'Unspecified'}",
            f"Risk score: {round(a['score'])}/100",
            f"Cases: {a['recent_cases']} in {a['recent_patients']} patients over "
            f"{config.RECENT_WINDOW_DAYS} days",
            f"Endemic expectation: {a['expected_cases']} ({a['excess_ratio']}x)",
        ]
        if reasons:
            lines.append("Why: " + "; ".join(reasons[:2]))
        lines.append("Recommended action: " + action)
        body = "\n".join(lines)
        subject = (f"Bioguard AI - {a['level']} outbreak risk: "
                   f"{a['pathogen_display']}")
        return {
            "active": active,
            "assessment": a,
            "pathogen": pk,
            "pathogen_display": a["pathogen_display"],
            "organism_full": a["full_name"],
            "ward": ward or "Unspecified",
            "other_wards": other_wards,
            "score": int(round(a["score"])),
            "score_raw": a["score"],
            "level": a["level"],
            "color": a["color"],
            "cases": a["recent_cases"],
            "patients": a["recent_patients"],
            "wards": a["recent_wards"],
            "expected": a["expected_cases"],
            "excess_ratio": a["excess_ratio"],
            "mdr_rate": a["mdr_rate"],
            "trend": trend,
            "trend_peak": peak,
            "trend_total": total7,
            "agents": agents,
            "recommendation": recommendation,
            "actions": a.get("actions", []),
            "reasons": reasons,
            "components": a["components"],
            "subject": subject,
            "body": body,
            "mailto": "mailto:?" + urlencode({"subject": subject, "body": body}),
            "whatsapp": "https://wa.me/?" + urlencode({"text": body}),
        }

    # ---- charts ----------------------------------------------------------
    @cached_property
    def chart_payload(self) -> dict:
        """Everything the front-end charts need, in one JSON blob."""
        tl = self.timeline
        return {
            "as_of": self.ref.isoformat(),
            "window_days": config.RECENT_WINDOW_DAYS,
            "timeline": {
                "labels": tl["labels"], "total": tl["total"],
                "patients": tl["patients"], "mdr": tl["mdr"],
                "series": tl["series"],
            },
            "risk": [{"label": a["pathogen_display"], "score": a["score"],
                      "level": a["level"], "color": a["color"]}
                     for a in self.outbreak_summary["ranked"]],
            "risk_mix": self.outbreak_summary["counts"],
            "amr_monthly": {
                "labels": self.amr_trends["labels"],
                "series": [{"label": display_name(pk),
                            "color": PATHOGENS[pk].color if pk in PATHOGENS else "#64748b",
                            "data": [p["rate"] for p in rows]}
                           for pk, rows in self.amr_trends["by_pathogen"].items()
                           if any(p["rate"] is not None for p in rows)],
            },
            "mdr_by_pathogen": [
                {"label": v["pathogen_display"], "mdr": v["mdr_rate"],
                 "isolates": v["isolates"]}
                for v in sorted(self.amr_summary["per_pathogen"].values(),
                                key=lambda x: -x["mdr_rate"])],
            "wards": [{"label": w["ward"], "isolates": w["isolates"],
                       "recent": w["recent"], "mdr": w["mdr_rate"]}
                      for w in self.ward_rows[:10]],
            # Painted onto the charts as reference lines, so a reader can see
            # *why* a bar is red rather than having to remember the policy.
            "thresholds": {"high": config.RISK_HIGH_THRESHOLD,
                           "medium": config.RISK_MEDIUM_THRESHOLD,
                           "alert_rate": round(config.AMR_ALERT_RATE * 100)},
            "scope": self.scope.label if self.scope else "",
        }

    def scoped_chart(self, pk: str) -> dict:
        """The same chart shapes, narrowed to one organism.

        The per-pathogen page reuses the front-end renderers rather than
        inventing a second vocabulary, so only the payload fed into
        :attr:`chart_payload` changes here - the drawing code is identical.
        """
        if pk not in PATHOGENS:
            return self.chart_payload
        subset = [i for i in self.isolates if i.pathogen == pk]
        meta = PATHOGENS[pk]
        tl = trends.timeline(subset, self.ref)
        at = amr.trends(subset, self.ref)
        burden = amr.burden(subset)
        assessment = next((a for a in self.outbreak_summary["assessments"]
                           if a.pathogen == pk), None)
        payload = dict(self.chart_payload)
        payload.update({
            "timeline": {"labels": tl["labels"], "total": tl["total"],
                         "patients": tl["patients"], "mdr": tl["mdr"],
                         "series": tl["series"]},
            "cases_note": meta.short_name,
            "risk": [{"label": assessment.pathogen_display,
                      "score": round(assessment.score, 1),
                      "level": assessment.level, "color": assessment.color}]
                     if assessment else [],
            "risk_mix": {"High": 0, "Medium": 0, "Low": 0},
            "amr_monthly": {
                "labels": at["labels"],
                "series": [{"label": meta.short_name, "color": meta.color,
                            "data": [p["rate"] for p in at["by_pathogen"].get(pk, [])]}],
            },
            "mdr_by_pathogen": [{"label": meta.short_name,
                                 "mdr": burden.get("mdr_rate", 0),
                                 "isolates": burden.get("isolates", 0)}],
            "wards": [{"label": w["ward"], "isolates": w["isolates"],
                       "recent": w["recent"], "mdr": w["mdr_rate"]}
                      for w in trends.ward_matrix(subset, self.ref)[:10]],
            "scope": f"{display_name(pk)} \u00b7 {self.scope.label}" if self.scope
            else display_name(pk),
        })
        return payload

    # ---- per-pathogen page -----------------------------------------------
    def pathogen_detail(self, pk: str) -> dict | None:
        """Everything knowable about one organism inside the current scope."""
        if pk not in PATHOGENS:
            return None
        subset = [i for i in self.isolates if i.pathogen == pk]
        meta = PATHOGENS[pk]
        trend = self.pathogen_trends.get(pk)
        assessment = next((a for a in self.outbreak_summary["assessments"]
                           if a.pathogen == pk), None)
        return {
            "key": pk,
            "name": meta.name,
            "short_name": meta.short_name,
            "group": meta.group,
            "gram": meta.gram,
            "color": meta.color,
            "phenotype": meta.phenotype,
            "parent": display_name(meta.parent) if meta.parent else "",
            "mdro": meta.mdro,
            "public_health": meta.public_health,
            "contact_precautions": meta.contact_precautions,
            "reservoir": meta.reservoir,
            "syndromes": list(meta.common_syndromes),
            "alert_text": meta.alert_text,
            "severity": meta.severity,
            "transmissibility": meta.transmissibility,
            "threat": meta.threat,
            "intrinsic": sorted(display_name(a) for a in meta.intrinsic),
            "critical_drugs": sorted(display_name(a) for a in meta.critical_drugs),
            "present": bool(subset),
            "isolates": subset,
            "burden": amr.burden(subset) if subset else {},
            "agents": amr.agent_rows(subset, min_tests=1) if subset else [],
            "trend": trend.as_dict() if trend else None,
            "assessment": assessment.as_dict() if assessment else None,
            "clusters": [c.as_dict() for c in
                         trends.detect_clusters(subset, self.ref)] if subset else [],
            "ward_clusters": [c.as_dict() for c in
                              trends.ward_clusters(subset, self.ref)] if subset else [],
            "patients": self._patient_rows(subset),
            "panel": amr.panel_for(pk),
        }

    @staticmethod
    def _patient_rows(subset, limit: int = 40) -> list[dict]:
        from .analysis.amr import classify_isolate
        by_patient: dict[str, list] = {}
        for iso in subset:
            by_patient.setdefault(iso.patient_id, []).append(iso)
        rank = ("", "Non-MDR resistant", "MDR", "XDR", "PDR")
        rows = []
        for pid, items in by_patient.items():
            items.sort(key=lambda i: i.sample_date)
            worst = max((classify_isolate(i).label or "" for i in items),
                        key=rank.index)
            rows.append({
                "patient_id": pid,
                "n": len(items),
                "wards": sorted({i.ward for i in items if i.ward}),
                "specimens": sorted({i.specimen_type for i in items}),
                "first": items[0].sample_date,
                "last": items[-1].sample_date,
                "class": worst or "Susceptible",
                "drugs": sorted({display_name(d) for i in items
                                 for d in i.resistant_drugs}),
                "report_id": items[-1].report_id,
            })
        rows.sort(key=lambda r: (-r["n"], r["last"]))
        return rows[:limit]

    # ---- isolate browser --------------------------------------------------
    def isolate_rows(self, limit: int = 500, offset: int = 0,
                     search: str = "") -> dict:
        """Filterable table of raw isolates - the audit trail behind every chart."""
        needle = (search or "").strip().lower()
        rows = list(self.everything)
        if needle:
            rows = [i for i in rows if needle in " ".join((
                i.patient_id, i.ward, i.specimen_type, i.organism_raw,
                i.pathogen, i.matched_phrase, " ".join(i.markers))).lower()]
        rows.sort(key=lambda i: (i.sample_date, i.id), reverse=True)
        from .analysis.amr import classify_isolate
        page = rows[offset:offset + limit]
        out = []
        for i in page:
            cls = classify_isolate(i)
            out.append({
                "id": i.id, "patient_id": i.patient_id, "ward": i.ward,
                "date": i.sample_date, "specimen": i.specimen_type,
                "organism_raw": i.organism_raw,
                "pathogen": i.pathogen,
                "pathogen_display": display_name(i.pathogen) if i.pathogen
                else (i.other_label or "Not a target organism"),
                "confidence": round(i.confidence, 2),
                "method": i.match_method,
                "markers": i.markers,
                "genus_only": i.genus_only,
                "class": cls.label or ("Susceptible" if i.sensitivities else ""),
                "resistant": [display_name(d) for d in i.resistant_drugs],
                "n_tested": len([s for s in i.sensitivities if s.result]),
                "report_id": i.report_id,
                "notes": i.notes,
            })
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": out}


def build(conn, scope, isolates, all_rows=None) -> Insights:
    return Insights(conn=conn, scope=scope, isolates=isolates, all_rows=all_rows)


def _hc(ward: str) -> bool:
    from .analysis.common import is_high_consequence_ward
    return is_high_consequence_ward(ward)
