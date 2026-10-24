"""Repoint registry references from the Day-3 local MLflow runs to their DagsHub copies.

The bootstrap champion was trained before DagsHub was configured, so `model_registry_events`
(events 1-3) and `models/champion.json` carry the local run id. The runs were copied to DagsHub
on 2026-10-24 (tag `migrated_from_local_run_id` on the copy). This replaces the id and keeps the
old one in `metrics.mlflow_local_run_id`. Idempotent: a second run finds nothing to change.
Dry run unless --apply. Never prints connection strings.

    uv run python scripts/repoint_mlflow_run_ids.py [--apply]
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from gridcast import db
from gridcast.jobs.predict import CHAMPION_KEY
from gridcast.storage import objstore

# local run id -> DagsHub run id (DATA_NOTES §19.4)
MAPPING = {
    "00560ff0f3cd4ca7ada0c86d28764272": "19dc3a899b1b4f1eb895c43274f0d7f5",  # gridcast-train
    "5279075b2a8748f0991e3138779b3ef6": "aecef0f962014095965dd7854ab176be",  # gridcast-backtest
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = db.make_engine(db.jobs_url())
    store = objstore.private_store()
    try:
        with engine.begin() as conn:
            for old, new in MAPPING.items():
                ids = [
                    r[0]
                    for r in conn.execute(
                        text("SELECT event_id FROM model_registry_events WHERE mlflow_run_id = :o"),
                        {"o": old},
                    )
                ]
                print(f"events with {old}: {ids}")
                if args.apply and ids:
                    conn.execute(
                        text(
                            "UPDATE model_registry_events SET mlflow_run_id = :n, metrics ="
                            " coalesce(metrics, '{}'::jsonb) || jsonb_build_object("
                            "'mlflow_local_run_id', CAST(:o AS text)) WHERE mlflow_run_id = :o"
                        ),
                        {"o": old, "n": new},
                    )
        champion = store.get_json(CHAMPION_KEY)
        old_id = champion.get("mlflow_run_id")
        print(f"champion.json mlflow_run_id: {old_id}")
        if args.apply and old_id in MAPPING:
            store.put_json(CHAMPION_KEY, {**champion, "mlflow_run_id": MAPPING[old_id]})
            print(f"champion.json -> {MAPPING[old_id]}")
    finally:
        engine.dispose()
    print("applied" if args.apply else "dry run (use --apply)")


if __name__ == "__main__":
    main()
