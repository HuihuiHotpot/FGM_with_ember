"""
Run dense Ember refinement windows.

Late-igniting cases continue from extracted coarse profiles. Early-igniting
cases restart directly from the inert mixing-line profile, while keeping the
same refine end time, global timestep, and profile sampling interval from
Q_peak_metrics.csv.
"""

import csv
import fnmatch
import math
import os
import re
import shutil
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import cantera as ct
import h5py
from ember import *

ct.suppress_thermo_warnings()


# ==================== Shared physical settings ====================

fuel = "C10H22:0.5088, C8H18:0.1141, MCH:0.1667, C7H8:0.2104"
oxidizer = "N2:3.76, O2:1.0"
Tfuel = 373


# ==================== User-editable run settings ====================

n_workers = 3
n_threads = 10
heartbeat_interval = 60

global_timestep = 1.0e-8

RESTART_ROOT = Path(r"F:\profiles_before_refine_start")
MIXING_RESTART_ROOT = Path(r"E:\FGM\run\initial_mixing_line_no_reactions")
REFINE_ROOT = Path(r"F:\profiles_refine_q10_window")
METRICS_CSV = Path(r"F:\Q_peak_metrics.csv")

# Cases whose dense window starts before this time are recomputed from the
# inert mixing-line profile instead of continuing from a coarse ignition profile.
RESTART_MIN_REFINE_START_MS = 1.0

case_selection = "all"  # "all" or "failed"
case_glob = "*"
clean_output_dirs = case_selection == "failed"


# ==================== Implementation ====================

case_pattern = re.compile(
    r"^T=(?P<T>[-+0-9.eE]+) K_p=(?P<p_bar>[-+0-9.eE]+) bar_a=(?P<a>[-+0-9.eE]+) s-1$"
)

status_header = [
    "time",
    "case_id",
    "case",
    "restart_mode",
    "status",
    "seconds",
    "restart_time_s",
    "t_end_s",
    "global_timestep",
    "suggested_profile_dt_s",
    "profile_step_interval",
    "actual_profile_dt_s",
    "restart_file",
    "output",
    "message",
]


def console_print(message):
    print(message, flush=True)


def final_numbered_profile(output_dir):
    profile_files = sorted(output_dir.glob("prof[0-9][0-9][0-9][0-9][0-9][0-9].h5"))
    if not profile_files:
        return None
    return profile_files[-1]


def read_profile_time_s(profile_path):
    with h5py.File(profile_path, "r") as handle:
        if "t" not in handle:
            raise KeyError(f"{profile_path} is missing root dataset 't'")
        dataset = handle["t"]
        if dataset.shape != ():
            raise ValueError(f"{profile_path} dataset 't' is not scalar; shape={dataset.shape}")
        return float(dataset[()])


def read_metrics(metrics_csv):
    if not metrics_csv.exists():
        raise FileNotFoundError(f"Metrics CSV not found: {metrics_csv}")

    metrics = {}
    with metrics_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {
            "case",
            "suggested_refine_start_ms",
            "suggested_refine_end_ms",
            "suggested_profile_dt_s",
        }
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Metrics CSV is missing columns: {sorted(missing)}")

        for row in reader:
            if row.get("status", "ok") != "ok":
                continue
            case = row["case"]
            metrics[case] = {
                "refine_start_ms": float(row["suggested_refine_start_ms"]),
                "t_end_s": float(row["suggested_refine_end_ms"]) * 1.0e-3,
                "suggested_profile_dt_s": float(row["suggested_profile_dt_s"]),
            }

    return metrics


def profile_step_interval_from_dt(profile_dt_s, global_dt_s):
    if profile_dt_s <= 0.0:
        raise ValueError(f"suggested_profile_dt_s must be positive, got {profile_dt_s}")
    return max(1, math.floor(profile_dt_s / global_dt_s))


def restart_mode_from_refine_start(refine_start_ms):
    if refine_start_ms >= RESTART_MIN_REFINE_START_MS:
        return "restart_from_coarse_profile"
    return "rerun_from_mixing_line"


def find_restart_file(case_name, restart_root):
    case_dir = Path(restart_root) / case_name
    if not case_dir.is_dir():
        return None
    return final_numbered_profile(case_dir)


def find_cases(restart_root, mixing_restart_root, case_glob, metrics, global_dt_s):
    restart_root = Path(restart_root)
    if not restart_root.exists():
        raise FileNotFoundError(f"Restart profile directory not found: {restart_root}")
    mixing_restart_root = Path(mixing_restart_root)
    if not mixing_restart_root.exists():
        raise FileNotFoundError(f"Mixing-line restart directory not found: {mixing_restart_root}")

    cases = []
    for case_name in sorted(metrics):
        if not fnmatch.fnmatchcase(case_name, case_glob):
            continue

        match = case_pattern.match(case_name)
        if not match:
            console_print(f"Skipping case with unexpected name: {case_name}")
            continue

        metric = metrics[case_name]
        restart_mode = restart_mode_from_refine_start(metric["refine_start_ms"])

        if restart_mode == "restart_from_coarse_profile":
            restart_file = find_restart_file(case_name, restart_root)
            console_print(f"Using coarse ignition restart for {case_name}: {restart_file}")
            if restart_file is None:
                console_print(
                    f"Falling back to mixing-line restart because no coarse restart profile "
                    f"was found: {case_name}"
                )
                restart_mode = "rerun_from_mixing_line"
            else:
                restart_time_s = read_profile_time_s(restart_file)

        if restart_mode == "rerun_from_mixing_line":
            restart_file = find_restart_file(case_name, mixing_restart_root)
            console_print(f"Rerunning from inert mixing-line profile for {case_name}: {restart_file}")
            if restart_file is None:
                console_print(
                    f"Skipping case without mixing-line profXXXXXX.h5: "
                    f"{mixing_restart_root / case_name}"
                )
                continue
            # The inert mixing-line profile's internal clock is not the ignition
            # transient clock, so direct reruns start at ignition time zero.
            restart_time_s = 0.0

        t_end_s = metric["t_end_s"]
        suggested_profile_dt_s = metric["suggested_profile_dt_s"]
        profile_step_interval = profile_step_interval_from_dt(
            suggested_profile_dt_s,
            global_dt_s,
        )
        actual_profile_dt_s = profile_step_interval * global_dt_s

        Toxidizer = float(match.group("T"))
        p_bar = float(match.group("p_bar"))
        a = float(match.group("a"))

        cases.append(
            (
                case_name,
                restart_mode,
                str(restart_file),
                restart_time_s,
                t_end_s,
                suggested_profile_dt_s,
                profile_step_interval,
                actual_profile_dt_s,
                Toxidizer,
                p_bar,
                a,
            )
        )

    return cases


def last_progress_line(log_file):
    if not log_file.exists():
        return ""

    with log_file.open("r", encoding="utf-8", errors="ignore") as handle:
        lines = handle.readlines()[-500:]

    for line in reversed(lines):
        line = line.strip()
        if "Runtime:" in line:
            return line

    for line in reversed(lines):
        line = line.strip()
        if "Continuing integration" in line:
            return line

    return ""


def case_completed(case_dir):
    for log_file in case_dir.glob("*.log"):
        text = log_file.read_text(encoding="utf-8", errors="ignore")
        if "Runtime:" in text:
            return True
    return False


def write_error_file(output_dir, case_name, exc):
    output_dir.mkdir(parents=True, exist_ok=True)
    error_file = output_dir / f"{case_name}__pid{os.getpid()}__error.txt"
    error_file.write_text(
        traceback.format_exc(),
        encoding="utf-8",
        errors="replace",
    )
    return error_file


def remove_case_output_dir(output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)


def run_one_case(task):
    (
        case_id,
        case_name,
        restart_mode,
        restart_file,
        restart_time_s,
        t_end_s,
        suggested_profile_dt_s,
        profile_step_interval,
        actual_profile_dt_s,
        Toxidizer,
        p_bar,
        a,
        n_threads,
        heartbeat_interval,
        global_timestep,
        refine_root,
        clean_output_dir,
    ) = task

    pressure = p_bar * 1e5
    output_dir = Path(refine_root) / case_name
    log_file = output_dir / f"{case_name}__pid{os.getpid()}.log"

    start_time = time.time()
    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(heartbeat_interval):
            elapsed_min = (time.time() - start_time) / 60.0
            progress = last_progress_line(log_file)
            if progress:
                console_print(f"Still running case {case_id}: {case_name} ({elapsed_min:.1f} min): {progress}")
            else:
                console_print(f"Still running case {case_id}: {case_name} ({elapsed_min:.1f} min), log: {log_file}")

    try:
        if clean_output_dir:
            remove_case_output_dir(output_dir)

        if case_completed(output_dir):
            return {
                "case_id": case_id,
                "case": case_name,
                "restart_mode": restart_mode,
                "status": "SKIPPED_COMPLETED",
                "seconds": 0.0,
                "restart_time_s": restart_time_s,
                "t_end_s": t_end_s,
                "global_timestep": global_timestep,
                "suggested_profile_dt_s": suggested_profile_dt_s,
                "profile_step_interval": profile_step_interval,
                "actual_profile_dt_s": actual_profile_dt_s,
                "restart_file": restart_file,
                "output": str(output_dir),
                "message": "case already completed",
            }

        output_dir.mkdir(parents=True, exist_ok=True)
        console_print(f"Running case {case_id}: {case_name}")

        conf = Config(
            Paths(outputDir=str(output_dir),
                  logFile=str(log_file)),
            Chemistry(mechanismFile="mechanism.yaml",
                      transportModel="UnityLewis",
                      kineticsModel="standard"),
            InitialCondition(restartFile=str(restart_file),
                             flameType="diffusion",
                             fuel=fuel,
                             oxidizer=oxidizer,
                             Tfuel=Tfuel,
                             Toxidizer=Toxidizer,
                             pressure=pressure,
                             equilibrateCounterflow=False),
            StrainParameters(initial=a,
                             final=a),
            General(nThreads=n_threads,
                    chemistryIntegrator="cvode"),
            Times(tStart=restart_time_s,
                  globalTimestep=global_timestep,
                  profileStepInterval=profile_step_interval),
            TerminationCondition(tEnd=t_end_s,
                                 measurement=None),
            Debug(timesteps=False))

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        conf.run()
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)

        if not case_completed(output_dir):
            raise RuntimeError("solver finished, but output validation failed")

        return {
            "case_id": case_id,
            "case": case_name,
            "restart_mode": restart_mode,
            "status": "SUCCESS",
            "seconds": time.time() - start_time,
            "restart_time_s": restart_time_s,
            "t_end_s": t_end_s,
            "global_timestep": global_timestep,
            "suggested_profile_dt_s": suggested_profile_dt_s,
            "profile_step_interval": profile_step_interval,
            "actual_profile_dt_s": actual_profile_dt_s,
            "restart_file": restart_file,
            "output": str(output_dir),
            "message": "",
        }

    except Exception as exc:
        stop_heartbeat.set()
        error_file = write_error_file(output_dir, case_name, exc)
        return {
            "case_id": case_id,
            "case": case_name,
            "restart_mode": restart_mode,
            "status": "FAILED",
            "seconds": time.time() - start_time,
            "restart_time_s": restart_time_s,
            "t_end_s": t_end_s,
            "global_timestep": global_timestep,
            "suggested_profile_dt_s": suggested_profile_dt_s,
            "profile_step_interval": profile_step_interval,
            "actual_profile_dt_s": actual_profile_dt_s,
            "restart_file": restart_file,
            "output": str(output_dir),
            "message": f"{type(exc).__name__}: {exc}; details: {error_file}",
        }


def append_status(status_file, result):
    with status_file.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            result["case_id"],
            result["case"],
            result["restart_mode"],
            result["status"],
            f"{result['seconds']:.3f}",
            f"{result['restart_time_s']:.16g}",
            f"{result['t_end_s']:.16g}",
            f"{result['global_timestep']:.3e}",
            f"{result['suggested_profile_dt_s']:.16g}",
            result["profile_step_interval"],
            f"{result['actual_profile_dt_s']:.16g}",
            result["restart_file"],
            result["output"],
            result["message"],
        ])


def ensure_status_file(status_file):
    status_file.parent.mkdir(parents=True, exist_ok=True)
    if not status_file.exists():
        with status_file.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(status_header)
        return

    with status_file.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        existing_header = reader.fieldnames or []
        rows = list(reader)

    if existing_header == status_header:
        return

    with status_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=status_header)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in status_header})


def latest_status_by_case(status_file):
    if not status_file.exists():
        raise FileNotFoundError(f"Status file not found for failed-case selection: {status_file}")

    latest = {}
    with status_file.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if "case" not in fieldnames or "status" not in fieldnames:
            raise KeyError(f"Status file is missing required columns 'case'/'status': {status_file}")
        for row in reader:
            case_name = row["case"]
            if not case_name:
                raise ValueError(f"Empty case name in status file: {status_file}")
            latest[case_name] = row

    return latest


def select_cases_by_status(cases, status_file, selection):
    if selection == "all":
        return cases
    if selection != "failed":
        raise ValueError(f"Unsupported case_selection={selection!r}; use 'all' or 'failed'")

    latest_status = latest_status_by_case(status_file)
    return [
        case
        for case in cases
        if latest_status.get(case[0], {}).get("status") == "FAILED"
    ]


if __name__ == "__main__":
    status_file = REFINE_ROOT / "_case_status.csv"

    metrics = read_metrics(METRICS_CSV)
    cases = find_cases(
        RESTART_ROOT,
        MIXING_RESTART_ROOT,
        case_glob,
        metrics,
        global_timestep,
    )
    cases = select_cases_by_status(cases, status_file, case_selection)

    console_print(f"Total selected refine cases: {len(cases)}")
    console_print(f"Case selection: {case_selection}")
    console_print(f"Global timestep: {global_timestep:.3e} s")
    console_print(f"Parallel settings: n_workers={n_workers}, n_threads={n_threads}")
    console_print(f"Restart root: {RESTART_ROOT}")
    console_print(f"Mixing-line restart root: {MIXING_RESTART_ROOT}")
    console_print(f"Output root: {REFINE_ROOT}")
    console_print(f"Metrics CSV: {METRICS_CSV}")
    console_print(f"Restart threshold: suggested_refine_start_ms >= {RESTART_MIN_REFINE_START_MS:g}")
    console_print(
        "Restart modes: "
        f"coarse={sum(1 for case in cases if case[1] == 'restart_from_coarse_profile')}, "
        f"mixing_line={sum(1 for case in cases if case[1] == 'rerun_from_mixing_line')}"
    )

    ensure_status_file(status_file)

    pending = []
    for case_id, case in enumerate(cases, start=1):
        (
            case_name,
            restart_mode,
            restart_file,
            restart_time_s,
            t_end_s,
            suggested_profile_dt_s,
            profile_step_interval,
            actual_profile_dt_s,
            Toxidizer,
            p_bar,
            a,
        ) = case
        pending.append((
            case_id,
            case_name,
            restart_mode,
            restart_file,
            restart_time_s,
            t_end_s,
            suggested_profile_dt_s,
            profile_step_interval,
            actual_profile_dt_s,
            Toxidizer,
            p_bar,
            a,
            n_threads,
            heartbeat_interval,
            global_timestep,
            str(REFINE_ROOT),
            clean_output_dirs,
        ))

    success_count = 0
    skipped_count = 0
    failed_count = 0

    with ProcessPoolExecutor(max_workers=n_workers, max_tasks_per_child=1) as executor:
        future_to_task = {executor.submit(run_one_case, task): task for task in pending}

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            (
                case_id,
                case_name,
                restart_mode,
                restart_file,
                restart_time_s,
                t_end_s,
                suggested_profile_dt_s,
                profile_step_interval,
                actual_profile_dt_s,
                Toxidizer,
                p_bar,
                a,
                n_threads,
                heartbeat_interval,
                global_timestep,
                refine_root_text,
                clean_output_dir,
            ) = task

            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "case_id": case_id,
                    "case": case_name,
                    "restart_mode": restart_mode,
                    "status": "FAILED",
                    "seconds": 0.0,
                    "restart_time_s": restart_time_s,
                    "t_end_s": t_end_s,
                    "global_timestep": global_timestep,
                    "suggested_profile_dt_s": suggested_profile_dt_s,
                    "profile_step_interval": profile_step_interval,
                    "actual_profile_dt_s": actual_profile_dt_s,
                    "restart_file": restart_file,
                    "output": str(Path(refine_root_text) / case_name),
                    "message": repr(exc) + "\n" + traceback.format_exc(),
                }

            append_status(status_file, result)

            if result["status"] == "SUCCESS":
                success_count += 1
                console_print(f"SUCCESS case {result['case_id']}: {result['case']}")
            elif result["status"] == "SKIPPED_COMPLETED":
                skipped_count += 1
                console_print(f"SKIPPED completed case {result['case_id']}: {result['case']}")
            else:
                failed_count += 1
                console_print(f"FAILED case {result['case_id']}: {result['case']}")

    console_print("All cases processed.")
    console_print(f"Summary: success={success_count}, skipped={skipped_count}, failed={failed_count}")
    console_print(f"Status file: {status_file}")
