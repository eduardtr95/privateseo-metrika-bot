from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc


def create_backup(
    database_path: Path,
    output_dir: Path,
    *,
    keep_days: int = 14,
    now: datetime | None = None,
) -> Path:
    if keep_days < 1:
        raise ValueError("keep_days must be positive")
    now = now or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    destination = output_dir / f"bot-{now.strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    temporary = output_dir / f".{destination.name}.tmp"

    try:
        with sqlite3.connect(database_path) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    cutoff = now - timedelta(days=keep_days)
    for candidate in output_dir.glob("bot-*.sqlite3"):
        if candidate == destination:
            continue
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if modified < cutoff:
            candidate.unlink()
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent Metrika bot SQLite backup")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--keep-days", default=14, type=int)
    args = parser.parse_args()
    destination = create_backup(args.database, args.output_dir, keep_days=args.keep_days)
    print(destination)


if __name__ == "__main__":
    main()
