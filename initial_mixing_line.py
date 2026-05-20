import csv
import os
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

# 在导入 Ember/Cantera/HDF5 相关库之前设置，减少 Windows 下 HDF5 文件锁问题。
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import cantera as ct
import numpy as np
from ember import *


def console_print(message):
    print(message, flush=True)


def last_progress_line(log_file):
    """从 log 末尾找最有用的一行进度信息。"""
    if not log_file.exists():
        return ""

    try:
        with log_file.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-500:]
    except OSError:
        return ""

    # 优先显示收敛判据。
    for line in reversed(lines):
        line = line.strip()
        if "||1/T * dT/dt||" in line:
            return line

    # 还没有收敛判据时，显示当前积分时间。
    for line in reversed(lines):
        line = line.strip()
        if "Continuing integration" in line:
            return line

    # 如果已经结束，显示结束信息。
    for line in reversed(lines):
        line = line.strip()
        if "Terminating integration" in line:
            return line

    return ""


def case_completed(case_dir):
    """判断 case 是否已经完整完成。"""
    for log_file in case_dir.glob("*.log"):
        text = log_file.read_text(encoding="utf-8", errors="ignore")
        if "Terminating integration:" in text and "Runtime:" in text:
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

def run_one_case(task):
    case_id, Toxidizer, p_bar, a, n_threads, heartbeat_interval, run_root = task

    gas = ct.Solution("mechanism_noreactions.yaml")

    fuel = "C10H22:0.5088, C8H18:0.1141, MCH:0.1667, C7H8:0.2104"
    oxidizer = "N2:3.76, O2:1.0"
    Tfuel = 373

    case_name = f"T={Toxidizer} K_p={p_bar} bar_a={a:g} s-1"
    output_dir = Path(run_root) / case_name
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
        if case_completed(output_dir):
            return {
                "case_id": case_id,
                "case": case_name,
                "status": "SKIPPED_COMPLETED",
                "seconds": 0.0,
                "output": str(output_dir),
                "message": "case already completed",
            }

        output_dir.mkdir(parents=True, exist_ok=True)

        console_print(f"Running case {case_id}: {case_name}")

        # ==================== 以下为原始脚本中的核心求解逻辑，保持不变 ====================
        output = str(output_dir)

        pressure = p_bar * 1e5

        xLeft = -0.01
        xRight = 0.01

        x = np.linspace(xLeft, xRight, 101)

        # 0 = fuel side, 1 = oxidizer side
        s = 0.5 * (1.0 + np.tanh(x / 0.001))

        T = Tfuel * (1.0 - s) + Toxidizer * s

        gas.TPX = Tfuel, pressure, fuel
        Yfuel = gas.Y

        gas.TPX = Toxidizer, pressure, oxidizer
        Yoxidizer = gas.Y

        Y = np.outer(Yfuel, 1.0 - s) + np.outer(Yoxidizer, s)

        rho = np.empty_like(x)
        for j in range(len(x)):
            gas.TPY = T[j], pressure, Y[:, j]
            rho[j] = gas.density

        rho_ox = rho[-1]
        U = a * np.sqrt(rho_ox / rho)
        V = np.zeros_like(x)

        j0 = np.argmin(np.abs(x))

        for j in range(j0 + 1, len(x)):
            dx = x[j] - x[j - 1]
            V[j] = V[j - 1] - 0.5 * (
                    rho[j] * U[j] + rho[j - 1] * U[j - 1]
            ) * dx

        for j in range(j0 - 1, -1, -1):
            dx = x[j + 1] - x[j]
            V[j] = V[j + 1] + 0.5 * (
                    rho[j] * U[j] + rho[j + 1] * U[j + 1]
            ) * dx

        conf = Config(
            Paths(outputDir=output,
                  logFile=str(log_file)),
            Chemistry(mechanismFile="mechanism_noreactions.yaml",
                      transportModel="UnityLewis"
                      ),
            InitialCondition(flameType="diffusion",
                             haveProfiles=True,
                             x=x,
                             T=T,
                             Y=Y,
                             U=U,
                             V=V,
                             pressure=pressure,
                             fuel=fuel,
                             oxidizer=oxidizer,
                             Tfuel=Tfuel,
                             Toxidizer=Toxidizer,
                             equilibrateCounterflow=False
                             ),
            StrainParameters(initial=a,
                             final=a),
            General(nThreads=n_threads,
                    chemistryIntegrator="qss"),
            Times(globalTimestep=1e-6,
                  profileStepInterval=2000),
            TerminationCondition(tEnd=10,
                                 measurement="dTdt"),
            Debug(timesteps=False))
        # ==================== 核心求解逻辑结束 ====================

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
            "output": str(output_dir),
            "message": f"{type(exc).__name__}: {exc}; details: {error_file}",
        }


if __name__ == "__main__":
    # ==================== 用户可修改的并行参数 ====================
    n_workers = 4
    n_threads = 8
    heartbeat_interval = 60

    run_root = Path(r"F:\FGM\run\initial_mixing_line_no_reactions")
    status_file = run_root / "_case_status.csv"

    # ==================== 与原始脚本一致的工况列表 ====================
    T_list = np.arange(400, 1250 + 1, 50)
    p_list_bar = [20, 60, 100, 150, 200, 250]

    # chist_factor由具体燃料的Zst给定，对于当前目标Zst=0.0638
    chist_factor = 0.0312

    low_a_points = np.array([1.0, 1.0 / chist_factor])
    main_a_points = np.geomspace(100.0, 3200.0, 9)

    a_list = np.unique(np.r_[low_a_points, main_a_points])

    cases = list(product(T_list, p_list_bar, a_list))
    console_print(f"Total number of cases: {len(cases)}")
    console_print(f"Parallel settings: n_workers={n_workers}, n_threads={n_threads}")

    run_root.mkdir(parents=True, exist_ok=True)
    if not status_file.exists():
        with status_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "case_id", "case", "status", "seconds", "output", "message"])

    tasks = []
    for case_id, (Toxidizer, p_bar, a) in enumerate(cases, start=1):
        tasks.append((case_id, Toxidizer, p_bar, a, n_threads, heartbeat_interval, str(run_root)))

    success_count = 0
    skipped_count = 0
    failed_count = 0

    # 每个 worker 只计算一个 case，结束后退出并释放 Ember/Cantera/HDF5
    # 相关的 C/C++ 层资源，避免长时间复用 worker 后内存累积。
    with ProcessPoolExecutor(max_workers=n_workers, max_tasks_per_child=1) as executor:
        future_to_task = {executor.submit(run_one_case, task): task for task in tasks}

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            case_id, Toxidizer, p_bar, a, _, _, _ = task

            try:
                result = future.result()
            except Exception as exc:
                case_name = f"T={Toxidizer} K_p={p_bar} bar_a={a:g} s-1"
                result = {
                    "case_id": case_id,
                    "case": case_name,
                    "status": "FAILED",
                    "seconds": 0.0,
                    "output": str(run_root / case_name),
                    "message": repr(exc) + "\n" + traceback.format_exc(),
                }

            with status_file.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    result["case_id"],
                    result["case"],
                    result["status"],
                    f"{result['seconds']:.3f}",
                    result["output"],
                    result["message"],
                ])

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
