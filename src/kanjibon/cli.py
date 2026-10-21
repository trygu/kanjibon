import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from .builder import build_sqlite


def _write_csv(output, rows):
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))


def export_data(
    db_path: Path, view: str, format: str, output_path: Path | None, limit: int | None
):
    """Export a database view to CSV or JSON."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # `view` is constrained by argparse choices, `limit` is an int
    query = f"SELECT * FROM {view}"
    if limit:
        query += f" LIMIT {limit}"

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if format == "csv":
        if output_path is None:
            _write_csv(sys.stdout, rows)
        else:
            with open(output_path, "w", newline="", encoding="utf-8") as output:
                _write_csv(output, rows)
            print(f"Exported {len(rows)} records to {output_path}")

    elif format == "json":
        data = [dict(row) for row in rows]
        json_str = json.dumps(data, ensure_ascii=False, indent=2)

        if output_path:
            output_path.write_text(json_str, encoding="utf-8")
            print(f"Exported {len(rows)} records to {output_path}")
        else:
            print(json_str)

    conn.close()


def _sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_manifest(output_dir: Path, db_path: Path, version: str | None = None):
    """Generate manifest.json with database quality metrics."""
    conn = sqlite3.connect(db_path)

    # Level distribution
    level_stats = {}
    for row in conn.execute("""
        SELECT lvl, COUNT(*) as total,
               COUNT(CASE WHEN freq IS NOT NULL THEN 1 END) as with_freq
        FROM kanji_seed GROUP BY lvl ORDER BY lvl DESC
    """):
        level_name = {5: "N5", 4: "N4", 3: "N3", 2: "N2", 1: "N1"}[row[0]]
        level_stats[level_name] = {"total": row[1], "with_frequency": row[2]}

    # Quality checks
    quality_checks = {}

    # Check for missing meanings
    no_meaning = conn.execute(
        "SELECT COUNT(*) FROM kanji_seed WHERE main_meaning IS NULL OR main_meaning = ''"
    ).fetchone()[0]
    quality_checks["kanji_without_meaning"] = no_meaning

    # Check for duplicates
    duplicates = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT literal, COUNT(*) FROM kanji_seed GROUP BY literal HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    quality_checks["duplicate_literals"] = duplicates

    conn.close()

    manifest = {
        "version": version or "local-build",
        "database": {
            "file": "kanjidic2.sqlite",
            "bytes": db_path.stat().st_size,
            "sha256": _sha256_file(db_path),
        },
        "jlpt_levels": level_stats,
        "quality_checks": quality_checks,
        "views": ["kanji_seed", "distractor_pool", "kanji_priority"],
        "app_contract": {
            "pick_random_kanji": "SELECT * FROM kanji_seed WHERE lvl=? ORDER BY RANDOM() LIMIT 1",
            "get_distractors": "SELECT meaning FROM distractor_pool WHERE lvl=? AND meaning<>? ORDER BY RANDOM() LIMIT 3",
        },
    }

    with open(output_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("Generated manifest.json with quality metrics")
    return manifest


def generate_artifacts(db_path: Path, output_dir: Path, version: str | None = None):
    """Generate quiz-focused production artifacts (database + manifest)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating quiz-focused artifacts in {output_dir}")

    # Copy database to output dir if different locations
    db_dest = output_dir / "kanjidic2.sqlite"
    if db_path.resolve() != db_dest.resolve():
        shutil.copy2(db_path, db_dest)
        print(f"Copied database to {db_dest}")
    else:
        print(f"Database already in place: {db_dest}")

    # Generate manifest with quality metrics
    manifest = generate_manifest(output_dir, db_path, version)

    print("✅ Quiz artifacts generated:")
    print(f"📁 Database: {db_dest}")
    print(
        f"📊 Quality checks passed: {all(v == 0 for v in manifest['quality_checks'].values())}"
    )
    print(f"🎯 JLPT levels available: {list(manifest['jlpt_levels'].keys())}")
    print("🛠️ App contract SQL queries included in manifest.json")


def app():
    ap = argparse.ArgumentParser(prog="kanjibon")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_build = sub.add_parser("build", help="Build SQLite from KANJIDIC2.xml")
    ap_build.add_argument("--input", "-i", type=Path, required=True)
    ap_build.add_argument("--db", "-o", type=Path, required=True)
    ap_build.add_argument("--batch", "-b", type=int, default=500)

    ap_export = sub.add_parser("export", help="Export data from SQLite to CSV/JSON")
    ap_export.add_argument(
        "--db", "-d", type=Path, required=True, help="SQLite database path"
    )
    ap_export.add_argument(
        "--view",
        "-v",
        choices=["kanji_seed", "kanji_priority"],
        default="kanji_seed",
        help="View to export",
    )
    ap_export.add_argument(
        "--format", "-f", choices=["csv", "json"], default="csv", help="Export format"
    )
    ap_export.add_argument(
        "--output", "-o", type=Path, help="Output file (default: stdout)"
    )
    ap_export.add_argument("--limit", "-l", type=int, help="Limit number of records")

    ap_artifacts = sub.add_parser("artifacts", help="Generate all production artifacts")
    ap_artifacts.add_argument(
        "--db", "-d", type=Path, required=True, help="SQLite database path"
    )
    ap_artifacts.add_argument(
        "--output-dir", "-o", type=Path, default=Path("output"), help="Output directory"
    )
    ap_artifacts.add_argument(
        "--version", "-v", type=str, help="Version string for manifest"
    )

    args = ap.parse_args()
    if args.cmd == "build":
        total = build_sqlite(args.input, args.db, batch_size=args.batch)
        print(f"Inserted {total} characters into {args.db}")
    elif args.cmd == "export":
        export_data(args.db, args.view, args.format, args.output, args.limit)
    elif args.cmd == "artifacts":
        generate_artifacts(args.db, args.output_dir, args.version)
