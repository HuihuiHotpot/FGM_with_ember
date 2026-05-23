from pathlib import Path
import re
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support

import h5py
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize


# ============================================================
# 用户设置
# ============================================================

root_dir = Path(r"E:\FGM\run\igniting_from_mixing_line")

save_dir = root_dir / "_T_profile_evolution_plots"
save_dir.mkdir(exist_ok=True)

profile_pattern = "prof[0-9][0-9][0-9][0-9][0-9][0-9].h5"

# 每个工况最多画多少条 profile
# 想更快可以改成 40 或 50
max_profiles_to_plot = 60

# 并行进程数
# 建议不要直接开满核心数，因为 matplotlib + h5py 同时开太多会抢磁盘
# 如果你的机器是 16 核，可以先试 6 或 8
num_workers = 16

# 已经存在图片时是否跳过
skip_existing = True

# 输出图片分辨率
# 300 更清晰但慢；200 通常足够看 scout
dpi = 200

# x 从 m 转成 mm
x_scale = 1000.0
x_label = "x [mm]"

# t 从 s 转成 ms
t_scale = 1000.0
t_label = "t [ms]"


# ============================================================
# 工具函数
# ============================================================

def profile_number(path: Path):
    m = re.fullmatch(r"prof(\d{6})\.h5", path.name)
    if m is None:
        raise ValueError(f"不是标准编号 profile 文件: {path}")
    return int(m.group(1))


def select_profile_files(profile_files, max_profile):
    """
    先从文件列表中抽取需要画的 profile 文件。
    不再先读取所有 h5 文件。
    """
    n = len(profile_files)

    if n <= max_profile:
        return profile_files

    idx = np.unique(
        np.linspace(0, n - 1, max_profile, dtype=int)
    )

    return [profile_files[i] for i in idx]


def read_profile(profile_file: Path):
    with h5py.File(profile_file, "r") as f:
        t = float(np.asarray(f["t"]))
        x = np.asarray(f["x"])
        T = np.asarray(f["T"])

    return t, x, T


def safe_name(name: str):
    s = name
    s = s.replace(" ", "_")
    s = s.replace("=", "")
    s = s.replace(",", "_")
    s = s.replace("(", "")
    s = s.replace(")", "")
    s = s.replace("/", "_")
    s = s.replace("\\", "_")
    s = s.replace(":", "_")
    s = s.replace("*", "_")
    s = s.replace("?", "_")
    s = s.replace('"', "_")
    s = s.replace("<", "_")
    s = s.replace(">", "_")
    s = s.replace("|", "_")
    return s


def plot_one_case(case_dir_str: str):
    """
    单个工况画图。
    这个函数会被多个进程并行调用。
    """
    case_dir = Path(case_dir_str)

    fig_name = safe_name(case_dir.name) + "_T_profile_evolution.png"
    fig_path = save_dir / fig_name

    if skip_existing and fig_path.exists():
        return f"[跳过已存在] {fig_path}"

    profile_files = sorted(
        case_dir.glob(profile_pattern),
        key=profile_number
    )

    if len(profile_files) == 0:
        return f"[跳过] 无标准编号 profile: {case_dir}"

    # 关键加速点：
    # 先抽取需要画的文件，再读取 h5
    profile_files = select_profile_files(
        profile_files,
        max_profiles_to_plot
    )

    times = []
    xs = []
    Ts = []

    for pf in profile_files:
        t, x, T = read_profile(pf)
        times.append(t)
        xs.append(x)
        Ts.append(T)

    times = np.asarray(times)

    order = np.argsort(times)
    times = times[order]
    xs = [xs[i] for i in order]
    Ts = [Ts[i] for i in order]

    t_plot = times * t_scale
    t_min = float(np.min(t_plot))
    t_max = float(np.max(t_plot))

    norm = Normalize(vmin=t_min, vmax=t_max)
    cmap = plt.get_cmap("viridis")

    fig, ax = plt.subplots(figsize=(8.0, 5.5))

    for i in range(len(times)):
        t_now = times[i] * t_scale
        x_now = xs[i] * x_scale
        T_now = Ts[i]

        ax.plot(
            x_now,
            T_now,
            linewidth=1.1,
            color=cmap(norm(t_now)),
            alpha=0.9
        )

    ax.set_xlabel(x_label)
    ax.set_ylabel("T [K]")
    ax.set_title(case_dir.name)
    ax.grid(True, alpha=0.3)

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])

    cbar = fig.colorbar(sm, ax=ax)
    cbar.set_label(t_label)

    fig.tight_layout()
    fig.savefig(fig_path, dpi=dpi)
    plt.close(fig)

    return f"[完成] {fig_path}"


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
    print(f"每个工况最多绘制 profile 数: {max_profiles_to_plot}")
    print(f"图片保存目录: {save_dir}")
    print()

    case_dir_strs = [str(p) for p in case_dirs]

    done = 0
    total = len(case_dir_strs)

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(plot_one_case, case_dir_str)
            for case_dir_str in case_dir_strs
        ]

        for future in as_completed(futures):
            done += 1
            msg = future.result()
            print(f"[{done}/{total}] {msg}")

    print()
    print("全部完成。")