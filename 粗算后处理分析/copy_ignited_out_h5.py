"""Copy out.h5 files for ignited Ember cases into one folder.

This is intentionally lightweight: it does not read HDF5 datasets or export
Q(t). It only uses the ignition-classification CSV to decide which case
directories to copy from.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from pathlib import Path


DEFAULT_RUN_ROOT = Path(r"E:\FGM\run\igniting_from_mixing_line")
DEFAULT_CLASSIFICATION_CSV = Path(r"F:\_ignition_classification_by_Tmax.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy out.h5 for cases with selected ignition statuses."
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=DEFAULT_RUN_ROOT,
        help="Root directory containing one subdirectory per Ember case.",
    )
    parser.add_argument(
        "--classification-csv",
        type=Path,
        default=DEFAULT_CLASSIFICATION_CSV,
        help="CSV containing at least columns: case, status.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination folder. Default: <run-root>/_ignited_out_h5.",
    )
    parser.add_argument(
        "--statuses",
        nargs="+",
        default=["normal_ignition"],
        help="Statuses to copy. Example: --statuses normal_ignition weak_ignition",
    )
    parser.add_argument(
        "--prefer-csv-case-dir",
        action="store_true",
        help="Use the case_dir column from the CSV when available.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite destination files if they already exist.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=20,
        help="Print progress every N selected cases. Use 1 for every case.",
    )
    return parser.parse_args()


def safe_filename(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', "_", text)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:180] or "case"


def read_classification_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"classification CSV not found: {path}")

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"case", "status"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"classification CSV is missing required columns: {sorted(missing)}"
            )
        return list(reader)


def source_out_h5(row: dict[str, str], run_root: Path, prefer_csv_case_dir: bool) -> Path:
    if prefer_csv_case_dir and row.get("case_dir"):
        return Path(row["case_dir"]) / "out.h5"
    return run_root / row["case"] / "out.h5"


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or (args.run_root / "_ignited_out_h5")
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_classification_rows(args.classification_csv)
    wanted_statuses = set(args.statuses)
    selected = [row for row in rows if row.get("status") in wanted_statuses]

    print(f"Classification CSV: {args.classification_csv}", flush=True)
    print(f"Run root: {args.run_root}", flush=True)
    print(f"Output dir: {output_dir}", flush=True)
    print(f"Statuses: {', '.join(args.statuses)}", flush=True)
    print(f"Selected cases: {len(selected)}", flush=True)

    copied = 0
    skipped_existing = 0
    failed = 0

    for case_number, row in enumerate(selected, start=1):
        case = row["case"]
        src = source_out_h5(row, args.run_root, args.prefer_csv_case_dir)
        dst = output_dir / f"{safe_filename(case)}__out.h5"

        if args.progress_every > 0 and (
            case_number == 1 or case_number % args.progress_every == 0
        ):
            print(f"[{case_number}/{len(selected)}] {case}", flush=True)

        try:
            if not src.exists():
                failed += 1
                print(f"  missing: {src}", flush=True)
                continue

            if dst.exists() and not args.overwrite:
                skipped_existing += 1
                continue

            shutil.copy2(src, dst)
            copied += 1
        except Exception as exc:
            failed += 1
            print(f"  failed: {case}: {type(exc).__name__}: {exc}", flush=True)

    print(f"Copied: {copied}", flush=True)
    print(f"Skipped existing: {skipped_existing}", flush=True)
    print(f"Failed/missing: {failed}", flush=True)
    print(f"Done: {output_dir}", flush=True)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
