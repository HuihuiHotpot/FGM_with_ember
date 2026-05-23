from pathlib import Path
import re
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support

import h5py
import numpy as np
import pandas as pd


# ============================================================
# 用户设置
# ============================================================

root_dir = Path(r"E:\FGM\run\igniting_from_mixing_line")

profile_pattern = "prof[0-9][0-9][0-9][0-9][0-9][0-9].h5"

output_csv = root_dir / "_ignition_classification_by_Tmax.csv"

# 判据设置
temperature_margin = 50.0     # K，避免数值扰动误判
normal_ignition_T = 2000.0    # K，超过该温度认为正常着火

# 氧化剂侧温度的确定方式
# "auto_hot_boundary": 取初始 profile 两端温度较高者，适合你的热氧化剂/冷燃料算例
# "right": 取初始 profile 最右端温度
# "left": 取初始 profile 最左端温度
oxidizer_temperature_mode = "auto_hot_boundary"

# 并行进程数
num_workers = min(8, max(1, os.cpu_count() - 1))


# ============================================================
# 工具函数
# ============================================================

def profile_number(path: Path):
    m = re.fullmatch(r"prof(\d{6})\.h5", path.name)
    if m is None:
        raise ValueError(f"不是标准编号 profile 文件: {path}")
    return int(m.group(1))


def get_reference_temperature_from_first_profile(profile_file: Path):
    """
    从初始 profile 估计氧化剂侧/热边界温度。

    对你的算例，通常一侧是冷燃料，一侧是热氧化剂。
    如果不想依赖左右方向，auto_hot_boundary 取两端较高温度。
    """
    with h5py.File(profile_file, "r") as f:
        T = np.asarray(f["T"])

    T_left = float(T[0])
    T_right = float(T[-1])

    if oxidizer_temperature_mode == "left":
        T_ref = T_left
    elif oxidizer_temperature_mode == "right":
        T_ref = T_right
    elif oxidizer_temperature_mode == "auto_hot_boundary":
        T_ref = max(T_left, T_right)
    else:
        raise ValueError(f"未知 oxidizer_temperature_mode: {oxidizer_temperature_mode}")

    return T_ref, T_left, T_right


def read_profile_t_x_Tmax(profile_file: Path):
    """
    读取单个 profile 的 t、Tmax、x_Tmax。
    """
    with h5py.File(profile_file, "r") as f:
        t = float(np.asarray(f["t"]))
        x = np.asarray(f["x"])
        T = np.asarray(f["T"])

    idx = int(np.argmax(T))
    Tmax = float(T[idx])
    x_Tmax = float(x[idx])

    return t, Tmax, x_Tmax


def classify_case(case_dir_str: str):
    case_dir = Path(case_dir_str)

    profile_files = sorted(
        case_dir.glob(profile_pattern),
        key=profile_number
    )

    if len(profile_files) == 0:
        return {
            "case": case_dir.name,
            "case_dir": str(case_dir),
            "status": "missing_profiles",
            "T_ref": np.nan,
            "T_left_initial": np.nan,
            "T_right_initial": np.nan,
            "Tmax_peak": np.nan,
            "t_Tmax_peak_ms": np.nan,
            "x_Tmax_peak_mm": np.nan,
            "n_profiles": 0,
            "remark": "No standard profXXXXXX.h5 files found."
        }

    T_ref, T_left, T_right = get_reference_temperature_from_first_profile(profile_files[0])

    Tmax_list = []
    t_list = []
    x_Tmax_list = []

    for pf in profile_files:
        t, Tmax, x_Tmax = read_profile_t_x_Tmax(pf)
        t_list.append(t)
        Tmax_list.append(Tmax)
        x_Tmax_list.append(x_Tmax)

    Tmax_arr = np.asarray(Tmax_list)
    t_arr = np.asarray(t_list)
    x_Tmax_arr = np.asarray(x_Tmax_list)

    i_peak = int(np.argmax(Tmax_arr))

    Tmax_peak = float(Tmax_arr[i_peak])
    t_Tmax_peak = float(t_arr[i_peak])
    x_Tmax_peak = float(x_Tmax_arr[i_peak])

    # ========================================================
    # 分类判据
    # ========================================================
    if Tmax_peak <= T_ref + temperature_margin:
        status = "non_ignited"
        remark = "Tmax does not exceed hot-side reference temperature beyond margin."
    elif Tmax_peak < normal_ignition_T:
        status = "weak_ignition"
        remark = "Tmax exceeds hot-side reference temperature, but remains below normal ignition threshold."
    else:
        status = "normal_ignition"
        remark = "Tmax exceeds normal ignition threshold."

    return {
        "case": case_dir.name,
        "case_dir": str(case_dir),
        "status": status,
        "T_ref": T_ref,
        "T_left_initial": T_left,
        "T_right_initial": T_right,
        "T_ref_plus_margin": T_ref + temperature_margin,
        "Tmax_peak": Tmax_peak,
        "Tmax_minus_Tref": Tmax_peak - T_ref,
        "t_Tmax_peak_ms": t_Tmax_peak * 1000.0,
        "x_Tmax_peak_mm": x_Tmax_peak * 1000.0,
        "n_profiles": len(profile_files),
        "remark": remark
    }


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    freeze_support()

    case_dirs = sorted(
        [
            p for p in root_dir.iterdir()
            if p.is_dir() and list(p.glob(profile_pattern))
        ]
    )

    print(f"找到 {len(case_dirs)} 个包含标准编号 profile 的工况文件夹。")
    print(f"并行进程数: {num_workers}")
    print(f"判据:")
    print(f"  未着火: Tmax_peak <= T_ref + {temperature_margin} K")
    print(f"  微弱着火: T_ref + {temperature_margin} K < Tmax_peak < {normal_ignition_T} K")
    print(f"  正常着火: Tmax_peak >= {normal_ignition_T} K")
    print()

    results = []
    total = len(case_dirs)

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(classify_case, str(case_dir))
            for case_dir in case_dirs
        ]

        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            results.append(result)

            print(
                f"[{done}/{total}] "
                f"{result['status']:>16s} | "
                f"Tmax={result['Tmax_peak']:.1f} K | "
                f"Tref={result['T_ref']:.1f} K | "
                f"{result['case']}"
            )

    df = pd.DataFrame(results)

    # 排序：先按状态，再按压力/温度等文件名排序
    df = df.sort_values(by=["status", "case"]).reset_index(drop=True)

    df.to_csv(output_csv, index=False, encoding="utf-8-sig")

    print()
    print("分类完成。")
    print(f"结果已保存到: {output_csv}")
    print()

    print("分类统计:")
    print(df["status"].value_counts())