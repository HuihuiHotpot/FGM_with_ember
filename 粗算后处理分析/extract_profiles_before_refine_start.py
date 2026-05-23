"""
Copy coarse-run profiles before each suggested refine-window start time.
"""

import csv
import os
import shutil
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import h5py

# ==================== User-editable settings ====================

RUN_ROOT = Path(r"E:\FGM\run\igniting_from_mixing_line")
OUTPUT_ROOT = Path(r"F:\profiles_before_refine_start")

PROFILE_GLOB = "prof[0-9][0-9][0-9][0-9][0-9][0-9].h5"
CONFIG_FILE_NAME = "config"
START_TIME_COLUMN = "suggested_refine_start_ms"
CASE_COLUMN = "case"
STATUS_COLUMN = "status"
REQUIRED_STATUS = "ok"

# Existing copied profile/config files are overwritten by default, so rerunning
# the script refreshes the copied pre-refine package.
OVERWRITE_EXISTING_FILES = True

# Copy the source CSV into OUTPUT_ROOT for traceability.
COPY_SOURCE_CSV = True

PROGRESS_EVERY_CASES = 10


# ==================== Implementation ====================


def console(message: str) -> None:
    print(message, flush=True)


def first_existing_path(candidates: list[Path]) -> Path:
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "None of the METRICS_CSV_CANDIDATES exists:\n"
        + "\n".join(f"  {path}" for path in candidates)
    )


def parse_float(text: str, field_name: str, case: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name!r} for case {case!r}: {text!r}") from exc


def find_case_dir(run_root: Path, case: str) -> Path | None:
    path = run_root / case
    if path.is_dir():
        return path
    return None


def read_profile_time_s(profile_path: Path) -> float:
    """Return the scalar time stored in the root dataset ``t``, in seconds."""
    with h5py.File(profile_path, "r") as handle:
        if "t" not in handle:
            raise KeyError(f"{profile_path} is missing root dataset 't'")
        dataset = handle["t"]
        if dataset.shape != ():
            raise ValueError(f"{profile_path} dataset 't' is not scalar; shape={dataset.shape}")
        return float(dataset[()])


def copy_one_case(
        case: str,
        case_dir: Path,
        output_case_dir: Path,
        refine_start_ms: float,
) -> dict[str, object]:
    refine_start_s = refine_start_ms * 1.0e-3
    profile_files = sorted(case_dir.glob(PROFILE_GLOB))
    if not profile_files:
        raise FileNotFoundError(f"no {PROFILE_GLOB} files found in {case_dir}")

    output_case_dir.mkdir(parents=True, exist_ok=True)
    selected_rows: list[list[object]] = []

    source_config = case_dir / CONFIG_FILE_NAME
    copied_config = output_case_dir / CONFIG_FILE_NAME
    if not source_config.exists():
        raise FileNotFoundError(f"case config not found: {source_config}")
    if not copied_config.exists() or OVERWRITE_EXISTING_FILES:
        shutil.copy2(source_config, copied_config)
        config_copy_status = "copied"
    else:
        config_copy_status = "skipped_existing"

    copied_count = 0
    skipped_existing_count = 0
    scanned_count = 0
    first_time_ms: float | None = None
    last_copied_time_ms: float | None = None
    next_profile_time_ms: float | None = None

    for profile_path in profile_files:
        scanned_count += 1
        profile_time_s = read_profile_time_s(profile_path)
        profile_time_ms = profile_time_s * 1.0e3

        if first_time_ms is None:
            first_time_ms = profile_time_ms

        if profile_time_s > refine_start_s:
            next_profile_time_ms = profile_time_ms
            break

        destination = output_case_dir / profile_path.name
        copied = False
        if destination.exists() and not OVERWRITE_EXISTING_FILES:
            skipped_existing_count += 1
        else:
            shutil.copy2(profile_path, destination)
            copied = True
            copied_count += 1

        last_copied_time_ms = profile_time_ms
        selected_rows.append(
            [
                case,
                profile_path.name,
                f"{profile_time_s:.16g}",
                f"{profile_time_ms:.16g}",
                str(profile_path),
                str(destination),
                "copied" if copied else "skipped_existing",
            ]
        )

    selected_profiles_csv = output_case_dir / "_selected_profiles.csv"
    with selected_profiles_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "case",
                "profile_file",
                "profile_time_s",
                "profile_time_ms",
                "source",
                "destination",
                "copy_status",
            ]
        )
        writer.writerows(selected_rows)

    return {
        "case": case,
        "case_dir": str(case_dir),
        "output_case_dir": str(output_case_dir),
        "refine_start_ms": refine_start_ms,
        "profiles_total_in_case_dir": len(profile_files),
        "profiles_scanned": scanned_count,
        "profiles_selected": len(selected_rows),
        "profiles_copied": copied_count,
        "profiles_skipped_existing": skipped_existing_count,
        "first_profile_time_ms": first_time_ms,
        "last_copied_profile_time_ms": last_copied_time_ms,
        "next_profile_time_ms": next_profile_time_ms,
        "selected_profiles_csv": str(selected_profiles_csv),
        "source_config": str(source_config),
        "copied_config": str(copied_config),
        "config_copy_status": config_copy_status,
        "message": "",
    }


def fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.16g}"
    return str(value)


def write_table(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([fmt(row.get(column)) for column in columns])


def main() -> int:
    metrics_csv = Path(r"F:\Q_peak_metrics.csv")
    if not RUN_ROOT.exists():
        raise FileNotFoundError(f"RUN_ROOT does not exist: {RUN_ROOT}")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    console(f"Metrics CSV: {metrics_csv}")
    console(f"Run root:    {RUN_ROOT}")
    console(f"Output root: {OUTPUT_ROOT}")

    if COPY_SOURCE_CSV:
        shutil.copy2(metrics_csv, OUTPUT_ROOT / "source_Q_peak_metrics.csv")

    with metrics_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {CASE_COLUMN, START_TIME_COLUMN}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"metrics CSV is missing columns: {sorted(missing)}")

        rows = [
            row
            for row in reader
            if not REQUIRED_STATUS
               or row.get(STATUS_COLUMN, REQUIRED_STATUS) == REQUIRED_STATUS
        ]

    summary_rows: list[dict[str, object]] = []
    failed_rows: list[dict[str, object]] = []

    for index, row in enumerate(rows, start=1):
        case = row[CASE_COLUMN]
        if index == 1 or index % PROGRESS_EVERY_CASES == 0:
            console(f"[{index}/{len(rows)}] {case}")

        try:
            refine_start_ms = parse_float(row[START_TIME_COLUMN], START_TIME_COLUMN, case)
            case_dir = find_case_dir(RUN_ROOT, case)
            if case_dir is None:
                raise FileNotFoundError(f"case directory not found under {RUN_ROOT}: {case}")

            output_case_dir = OUTPUT_ROOT / case_dir.name
            summary_rows.append(
                copy_one_case(
                    case=case_dir.name,
                    case_dir=case_dir,
                    output_case_dir=output_case_dir,
                    refine_start_ms=refine_start_ms,
                )
            )
        except Exception as exc:
            failed_rows.append(
                {
                    "case": case,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            console(f"  FAILED: {case}: {type(exc).__name__}: {exc}")

    summary_columns = [
        "case",
        "case_dir",
        "output_case_dir",
        "refine_start_ms",
        "profiles_total_in_case_dir",
        "profiles_scanned",
        "profiles_selected",
        "profiles_copied",
        "profiles_skipped_existing",
        "first_profile_time_ms",
        "last_copied_profile_time_ms",
        "next_profile_time_ms",
        "selected_profiles_csv",
        "source_config",
        "copied_config",
        "config_copy_status",
        "message",
    ]
    failed_columns = ["case", "error_type", "message"]
    write_table(OUTPUT_ROOT / "_copy_profiles_before_refine_summary.csv", summary_columns, summary_rows)
    write_table(OUTPUT_ROOT / "_copy_profiles_before_refine_failed.csv", failed_columns, failed_rows)

    total_selected = sum(int(row.get("profiles_selected", 0)) for row in summary_rows)
    total_copied = sum(int(row.get("profiles_copied", 0)) for row in summary_rows)
    total_skipped = sum(int(row.get("profiles_skipped_existing", 0)) for row in summary_rows)

    console("")
    console(f"Cases requested: {len(rows)}")
    console(f"Cases succeeded: {len(summary_rows)}")
    console(f"Cases failed:    {len(failed_rows)}")
    console(f"Profiles selected:         {total_selected}")
    console(f"Profiles copied:           {total_copied}")
    console(f"Profiles skipped existing: {total_skipped}")
    console(f"Summary: {OUTPUT_ROOT / '_copy_profiles_before_refine_summary.csv'}")
    console(f"Failed:  {OUTPUT_ROOT / '_copy_profiles_before_refine_failed.csv'}")

    return 0 if not failed_rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
