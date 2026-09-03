#!/usr/bin/env python3
"""
Bioguard AI - entry point.

    python app.py                 # serve on http://127.0.0.1:5000
    python app.py --port 8080 --host 0.0.0.0
    flask --app app seed-demo     # reload the demonstration dataset
    flask --app app reset-db      # delete everything

The first run on an empty database seeds a synthetic hospital dataset so the
dashboard opens on real analysis rather than a blank page; set
BIOGUARD_AUTOSEED=0 to skip that.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bioguard import create_app  # noqa: E402

app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Bioguard AI dashboard.")
    parser.add_argument("--host", default=os.environ.get("BIOGUARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", "-p", type=int,
                        default=int(os.environ.get("BIOGUARD_PORT", "5000")))
    parser.add_argument("--debug", action="store_true",
                        help="auto-reload on code changes")
    parser.add_argument("--no-reload", action="store_true",
                        help="disable the reloader (use with a debugger)")
    args = parser.parse_args()

    banner(app, args.host, args.port)
    app.run(host=args.host, port=args.port,
            debug=args.debug, use_reloader=args.debug and not args.no_reload)


def banner(flask_app, host: str, port: int) -> None:
    from bioguard import database, ingest
    conn = database.connect(flask_app.config["DB_PATH"])
    try:
        counts = database.database_counts(conn)
        seeded = ingest.demo_is_seeded(conn)
    finally:
        conn.close()
    line, dash = "=" * 68, "-" * 68
    print(f"""
{line}
  Bioguard AI - Pathogen Surveillance & Outbreak Intelligence
{line}
  dashboard   http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/
  database    {flask_app.config['DB_PATH']}
  uploads     {flask_app.config['UPLOAD_DIR']}
  {dash}
  reports     {counts.get('reports', 0):>7,}
  isolates    {counts.get('isolates', 0):>7,}   (target organisms: {counts.get('targets', 0):,})
  patients    {counts.get('patients', 0):>7,}   wards: {counts.get('wards', 0):,}
  sensitivities {counts.get('sensitivities', 0):>5,}
  window      {counts.get('first_date') or '-'} to {counts.get('last_date') or '-'}
{dash}
  demonstration dataset {'loaded' if seeded else 'not loaded'}
{line}
""")


if __name__ == "__main__":
    main()
