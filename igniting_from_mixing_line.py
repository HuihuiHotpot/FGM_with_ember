"""
Restart runs from completed inert mixing-line profiles.
"""

import csv
import os
import re
import shutil
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

from ember import *
import cantera as ct

ct.suppress_thermo_warnings()

fuel = "C10H22:0.5088, C8H18:0.1141, MCH:0.1667, C7H8:0.2104"
oxidizer = "N2:3.76, O2:1.0"
Tfuel = 373
status_header = [
    "time", "case_id", "case", "status", "seconds",
    "global_timestep", "output", "message"
]

case_pattern = re.compile(
    r"^T=(?P<T>[-+0-9.eE]+) K_p=(?P<p_bar>[-+0-9.eE]+) bar_a=(?P<a>[-+0-9.eE]+) s-1$"
)


def console_print(message):
    print(message, flush=True)


def final_numbered_profile(output_dir):
    profile_files = sorted(output_dir.glob("prof[0-9][0-9][0-9][0-9][0-9][0-9].h5"))
    if not profile_files:
        return None
    return profile_files[-1]


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


def find_cases(mixing_root, case_glob):
    mixing_root = Path(mixing_root)
    if not mixing_root.exists():
        raise FileNotFoundError(f"Mixing-line output directory not found: {mixing_root}")

    cases = []
    for case_dir in sorted(mixing_root.glob(case_glob)):
        if not case_dir.is_dir():
            continue

        match = case_pattern.match(case_dir.name)
        if not match:
            console_print(f"Skipping directory with unexpected name: {case_dir}")
            continue

        restart_file = final_numbered_profile(case_dir)
        if restart_file is None:
            console_print(f"Skipping case without numbered profXXXXXX.h5: {case_dir}")
            continue

        Toxidizer = float(match.group("T"))
        p_bar = float(match.group("p_bar"))
        a = float(match.group("a"))

        cases.append((case_dir.name, str(restart_file), Toxidizer, p_bar, a))

    return cases


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
        restart_file,
        Toxidizer,
        p_bar,
        a,
        n_threads,
        heartbeat_interval,
        t_end,
        global_timestep,
        ignition_root,
        clean_output_dir,
    ) = task

    pressure = p_bar * 1e5
    output_dir = Path(ignition_root) / case_name
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
                "status": "SKIPPED_COMPLETED",
                "seconds": 0.0,
                "global_timestep": global_timestep,
                "output": str(output_dir),
                "message": "case already completed",
            }

        output_dir.mkdir(parents=True, exist_ok=True)
        console_print(f"Running case {case_id}: {case_name}")

        # Core solve setup copied from igniting_from_mixing_line.py.
        output = str(output_dir)

        conf = Config(
            Paths(outputDir=output,
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
            Times(tStart=0.0,
                  globalTimestep=global_timestep,
                  profileStepInterval=int(round(5.0e-6 / global_timestep))),
            TerminationCondition(tEnd=t_end,
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
            "status": "SUCCESS",
            "seconds": time.time() - start_time,
            "global_timestep": global_timestep,
            "output": str(output_dir),
            "message": "",
        }

    except Exception as exc:
        stop_heartbeat.set()
        error_file = write_error_file(output_dir, case_name, exc)

        return {
            "case_id": case_id,
            "case": case_name,
            "status": "FAILED",
            "seconds": time.time() - start_time,
            "global_timestep": global_timestep,
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
            result["status"],
            f"{result['seconds']:.3f}",
            f"{result['global_timestep']:.3e}",
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


def select_cases_by_status(cases, status_file, case_selection):
    if case_selection == "all":
        return cases
    if case_selection != "failed":
        raise ValueError(f"Unsupported case_selection={case_selection!r}; use 'all' or 'failed'")

    latest_status = latest_status_by_case(status_file)
    return [
        case
        for case in cases
        if latest_status.get(case[0], {}).get("status") == "FAILED"
    ]


if __name__ == "__main__":
    # ==================== User-editable run settings ====================
    n_workers = 3
    n_threads = 10
    heartbeat_interval = 60
    t_end = 0.025
    global_timestep = 1.0e-6

    mixing_root = Path(r"F:\FGM\run\initial_mixing_line_no_reactions")
    ignition_root = Path(r"F:\FGM\run\igniting_from_mixing_line_3")
    status_file = ignition_root / "_case_status.csv"

    case_selection = "all"  # "all" or "failed"
    case_glob = "*"
    clean_output_dirs = case_selection == "failed"

    # ==================== Case discovery ====================
    cases = find_cases(
        mixing_root,
        case_glob,
    )
    cases = select_cases_by_status(cases, status_file, case_selection)

    console_print(f"Total selected restart cases: {len(cases)}")
    console_print(f"Case selection: {case_selection}")
    console_print(f"Global timestep: {global_timestep:.3e} s")
    console_print(f"Parallel settings: n_workers={n_workers}, n_threads={n_threads}")
    console_print(f"Output root: {ignition_root}")

    ensure_status_file(status_file)

    pending = []
    for case_id, case in enumerate(cases, start=1):
        case_name, restart_file, Toxidizer, p_bar, a = case
        pending.append((
            case_id,
            case_name,
            restart_file,
            Toxidizer,
            p_bar,
            a,
            n_threads,
            heartbeat_interval,
            t_end,
            global_timestep,
            str(ignition_root),
            clean_output_dirs,
        ))

    success_count = 0
    skipped_count = 0
    failed_count = 0

    # Run one case per worker process so Ember/Cantera/HDF5 resources are
    # released cleanly, matching the inert mixing-line driver.
    with ProcessPoolExecutor(max_workers=n_workers, max_tasks_per_child=1) as executor:
        future_to_task = {executor.submit(run_one_case, task): task for task in pending}

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            (
                case_id,
                case_name,
                restart_file,
                Toxidizer,
                p_bar,
                a,
                n_threads,
                heartbeat_interval,
                t_end,
                global_timestep,
                ignition_root_text,
                clean_output_dir,
            ) = task

            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "case_id": case_id,
                    "case": case_name,
                    "status": "FAILED",
                    "seconds": 0.0,
                    "global_timestep": global_timestep,
                    "output": str(Path(ignition_root_text) / case_name),
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
