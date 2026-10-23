"""One-off: copy the local Adequacy3 XML archive into the private bucket (plan §3.2).

Walks `<root>/ieso/adequacy3/YYYY/MM/DD/*.xml.gz`, uploads each file to
`raw/ieso/adequacy3/YYYY/MM/DD/<file>` unless an object with that key already exists, verifies the
round trip byte for byte, and records the key in `ieso_adequacy_files.archive_key`. Local files are
left in place (delete them by hand once the bucket copy is confirmed).

    uv run python scripts/copy_archive_fallback.py --root data/raw
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import text

from gridcast import db
from gridcast.ingest.ieso_adequacy import ARCHIVE_PREFIX
from gridcast.storage.objstore import private_store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/raw")
    args = ap.parse_args()
    base = Path(args.root) / "ieso" / "adequacy3"
    files = sorted(base.rglob("*.xml.gz"))
    store = private_store()
    existing = set(store.list_keys(ARCHIVE_PREFIX))
    engine = db.make_engine(db.jobs_url())
    uploaded = skipped = 0
    keys: dict[str, str] = {}
    for path in files:
        key = ARCHIVE_PREFIX + path.relative_to(base).as_posix()
        body = path.read_bytes()
        if key in existing:
            skipped += 1
        else:
            store.put_bytes(key, body, "application/gzip")
            if store.get_bytes(key) != body:
                raise RuntimeError(f"round trip mismatch for {key}")
            uploaded += 1
        keys[path.name.removesuffix(".gz")] = key
    with engine.begin() as conn:
        updated = 0
        for name, key in keys.items():
            res = conn.execute(
                text(
                    "UPDATE ieso_adequacy_files SET archive_key = :k"
                    " WHERE source_file = :f AND archive_key IS DISTINCT FROM :k"
                ),
                {"k": key, "f": name},
            )
            updated += int(res.rowcount or 0)
    in_bucket = sum(1 for _ in store.list_keys(ARCHIVE_PREFIX))
    print(
        f"local files: {len(files)}, uploaded: {uploaded}, already in bucket: {skipped}, "
        f"ledger rows updated: {updated}, objects under {ARCHIVE_PREFIX}: {in_bucket}"
    )


if __name__ == "__main__":
    main()
