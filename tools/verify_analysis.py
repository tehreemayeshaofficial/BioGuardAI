"""
End-to-end analytics smoke test.

Loads the seeded demo scenario through the real persistence layer
(:func:`bioguard.database.insert_records` + :func:`load_dataset`) and prints the
risk distribution, cluster list and AMR movements so the scoring model can be
sanity-checked without starting Flask.

Run from the project root::

    python tools/verify_analysis.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bioguard import database                              # noqa: E402
from bioguard.analysis import amr, outbreak, trends        # noqa: E402
from bioguard.analysis.common import reference_date        # noqa: E402
from bioguard.demo_data import CLUSTERS, build_records     # noqa: E402


def load(path: Path):
    """Parse -> persist -> re-load, exercising the real data path."""
    t0 = time.time()
    records = build_records()
    conn = database.connect(path)
    database.init_db(conn)
    rid = database.create_report(conn, filename="demo_scenario", source="demo")
    n_iso, n_sen = database.insert_records(conn, rid, records)
    isos = database.load_dataset(conn)
    print(f"records={len(records)} isolates={n_iso} sensitivities={n_sen} "
          f"loaded={len(isos)}  ({time.time() - t0:.2f}s)")
    return conn, isos


def report_risk(summ: dict, failures: list[str]) -> dict[str, dict]:
    ranked = summ["ranked"]
    print(f"\n{'pathogen':22}{'score':>7} {'base':>7} {'level':>7}  "
          f"{'cases':>5}{'pts':>5}{'exp':>6}{'x':>6}{'gate':>6}{'bins':>5}"
          f"{'mdr%':>7}  clus")
    dist: Counter[str] = Counter()
    for a in ranked:
        dist[a["level"]] += 1
        print(f"{a['pathogen_display']:22}{a['score']:7.1f} "
              f"{a['base_level']:>7} {a['level']:>7}  "
              f"{a['recent_cases']:5}{a['recent_patients']:5}"
              f"{a['expected_cases']:6.1f}{a['excess_ratio']:6.2f}"
              f"{a['endemic_gate']:6.2f}{a['baseline_bins']:5}"
              f"{a['mdr_rate']:7.1f}  {len(a['clusters'])}"
              + ("  DE" if a["de_escalated"] else ""))
    print("\ncounts:", summ["counts"], "->", dict(dist))

    if dist.get("High", 0) > 7:
        failures.append(f"too many High ({dist.get('High', 0)}) - model over-calls")
    if dist.get("High", 0) == 0:
        failures.append("no High at all - seeded outbreak not detected")
    if dist.get("Low", 0) == 0:
        failures.append("no Low at all - model never stands down")
    if len(ranked) != 12:
        failures.append(f"expected 12 pathogens, got {len(ranked)}")

    for a in ranked:
        if a["level"] != "High":
            continue
        print(f"\n  [High] {a['full_name']}  score={a['score']}")
        for r in a["escalation_reasons"]:
            print(f"     ! {r}")
        for c in a["components"]:
            print(f"     - {c['label']:33} {c['points']:5.1f}/{c['max_points']:<4} "
                  f"{c['detail']}")

    print("\n  [not High]")
    for a in ranked:
        if a["level"] == "High":
            continue
        print(f"  {a['level']:7}{a['pathogen_display']:19} score={a['score']:5.1f} "
              f"cases={a['recent_cases']:3} exp={a['expected_cases']:5.1f} "
              f"x={a['excess_ratio']:5.2f} gate={a['endemic_gate']:4.2f} "
              f"{a['escalation_reasons'] or ''}")
    return {a["pathogen"]: a for a in ranked}


def main() -> int:
    failures: list[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="bioguard_verify_"))
    try:
        conn, isos = load(tmpdir / "verify.db")
        try:
            ref = reference_date(isos)
            print(f"reference date: {ref}   tracked isolates: {len(isos)}")

            t0 = time.time()
            summ = outbreak.summary(isos, ref)
            print(f"\noutbreak.summary -> {summ['overall_level']} "
                  f"{summ['overall_score']}  ({time.time() - t0:.2f}s)")
            by_key = report_risk(summ, failures)

            # Seeded events that must not be dismissed as background noise.
            quiet = [c.pathogen for c in CLUSTERS
                     if by_key.get(c.pathogen, {}).get("level") == "Low"]
            if len(quiet) > 3:
                failures.append(f"{len(quiet)} seeded cluster pathogens rated Low: "
                                + ", ".join(quiet))
            print(f"\nseeded clusters rated Low: {quiet or '-'}")

            cl = trends.detect_clusters(isos)
            print(f"\nclusters: {len(cl)}")
            for c in sorted(cl, key=lambda x: -x.n_patients)[:10]:
                print(f"   {c.label:56} {c.start}..{c.end} ({c.span_days}d) "
                      f"mdr={c.mdr} wards={', '.join(c.wards)}")
            if len(cl) > 18:
                failures.append(f"{len(cl)} clusters - cluster detection too loose")

            wm = trends.ward_matrix(isos, ref)
            print(f"\nwards: {len(wm)}")
            for w in wm[:5]:
                print(f"   {w['ward']:22} iso={w['isolates']:4} pts={w['patients']:3} "
                      f"recent={w['recent']:3} mdr={w['mdr_rate']:5.1f}% "
                      f"top={w['top_pathogen']} ({w['top_share']}%)")

            print(f"\nward x pathogen units: {len(summ['ward_assessments'])}")
            for r in summ["ward_assessments"][:6]:
                print(f"   {r['ward']:20} {r['pathogen_display']:20} "
                      f"pts={r['n_patients']:2} score={r['score']:5.1f} {r['level']}")

            tr = trends.pathogen_trends(isos, ref)
            print(f"\npathogen trends: {len(tr)}")
            for t in sorted(tr.values(), key=lambda x: -x.total)[:6]:
                print(f"   {t.pathogen_display:20} total={t.total:4} "
                      f"recent={t.recent:3} base={t.baseline:3} "
                      f"{t.change_pct:+7.1f}% {t.direction:9} wards={t.wards} "
                      f"pts/wk={t.patients_per_week}")

            tl = trends.timeline(isos, ref)
            print(f"\ntimeline: {len(tl.get('labels', []))} buckets, "
                  f"series={len(tl.get('series', []))}, keys={sorted(tl)}")

            at = amr.trends(isos, ref)
            rising = sum(1 for m in at["movements"] if m["direction"] == "rising")
            print(f"\nAMR movements: {len(at['movements'])} (rising={rising})")
            for m in at["movements"][:8]:
                print(f"   {m['pathogen_display']:18} {m['antibiotic_display']:22} "
                      f"{m['from_rate']:5.1f}% -> {m['to_rate']:5.1f}% "
                      f"({m['delta_pp']:+.1f}pp) {m['direction']}")
            if rising < 3:
                failures.append(f"only {rising} rising AMR movements - "
                                "seeded trends lost")

            al = amr.alerts(isos)
            print(f"\nAMR alerts: {len(al)}")
            for x in al[:6]:
                print(f"   [{x['severity']:8}] {x['pathogen_display']:18} "
                      f"{x['antibiotic_display']:22} {x['rate']:5.1f}% "
                      f"(n={x['tested']}){' *critical*' if x['is_critical'] else ''}")

            s = amr.summary(isos, ref)
            print(f"\noverall MDR rate: {s['overall_mdr_rate']}%   "
                  f"last-line exhausted: {len(s['last_line_exhausted'])}")
            for v in s["ranked"][:6]:
                print(f"   {v['pathogen_display']:20} isolates={v['isolates']:4} "
                      f"MDR={v['mdr']:3} XDR={v['xdr']:3} PDR={v['pdr']:2} "
                      f"mdr_rate={v['mdr_rate']:5.1f}%  "
                      f"worst={v['top_agent']} {v['top_rate']}%")
            if not s["ranked"]:
                failures.append("AMR summary empty")
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + ("FAIL:\n  - " + "\n  - ".join(failures) if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
