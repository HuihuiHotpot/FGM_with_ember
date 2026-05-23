"""
Analyze Q-peak width metrics from copied Ember out.h5 files.
"""

import csv
import math
import re
from pathlib import Path

import h5py
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ==================== User-editable settings ====================

INPUT_DIR = Path(r"G:\FGM\igniting_from_mixing_line\粗算\后处理\_ignited_out_h5")
OUTPUT_DIR = Path(r"G:\FGM\igniting_from_mixing_line\粗算\后处理\_Q_peak_analysis")

# Thresholds are fractions of Qplus_max. For example, 0.5 means half maximum.
THRESHOLDS = [0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 0.80, 0.90]

# Suggested dense sampling interval:
#   target_points_across_width_50 = 20 means roughly 20 samples across FWHM.
TARGET_POINTS_ACROSS_WIDTH_50 = 30

# Optional bounds for suggested_profile_dt_s. Leave as None to report the
# value directly implied by width_Q0.5_s / TARGET_POINTS_ACROSS_WIDTH_50.
MIN_SUGGESTED_PROFILE_DT_S: float | None = None
MAX_SUGGESTED_PROFILE_DT_S: float | None = None

# Suggested refine window around the Q peak.
# Q05 is used for the dense refine window:
#   start = t_Q0.05_rise - WINDOW_MARGIN_MS
#   end   = last t_Q0.05_fall + WINDOW_MARGIN_MS
# If Q05 never falls back below the threshold, fall back to Q10 fall before
# using t_Qmax. This avoids cutting off the clearly resolved Q10 decay tail.
#
# Q50 is still used below for suggested_profile_dt_s through width_Q0.5_s.
WINDOW_LOW_THRESHOLD = 0.05
WINDOW_MARGIN_MS = 0.01
REFINE_END_FALLBACK_THRESHOLD = 0.10

# Plot outputs. The script writes one case plot per H5 file plus summary plots.
MAKE_CASE_PLOTS = True
CASE_PLOT_DIR = OUTPUT_DIR / "case_plots"
CASE_PLOT_FORMAT = "png"
CASE_PLOT_DPI = 180
MAX_CASE_PLOTS: int | None = None

# These levels are highlighted in each case plot.
PLOT_WIDTH_LEVELS = [0.50, 0.05]
PLOT_ZOOM_EXTRA_FRACTION = 0.25
PLOT_ZOOM_MIN_EXTRA_MS = 0.01


# ==================== Implementation ====================


def case_name_from_file(path: Path) -> str:
    name = path.stem
    if name.endswith("__out"):
        name = name[:-5]

    match = re.fullmatch(r"T=(.+)_K_p=(.+)_bar_a=(.+)_s-1", name)
    if match:
        T, p, a = match.groups()
        return f"T={T} K_p={p} bar_a={a} s-1"

    return name


def safe_filename(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', "_", text)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:180] or "case"


def interpolate_time(t0: float, y0: float, t1: float, y1: float, threshold: float) -> float:
    if y1 == y0:
        return t0
    fraction = (threshold - y0) / (y1 - y0)
    fraction = min(1.0, max(0.0, fraction))
    return t0 + fraction * (t1 - t0)


def first_rising_crossing(t: np.ndarray, y: np.ndarray, i_peak: int, threshold: float) -> float | None:
    if y[0] >= threshold:
        return float(t[0])

    for i in range(1, i_peak + 1):
        if y[i - 1] < threshold <= y[i]:
            return interpolate_time(float(t[i - 1]), float(y[i - 1]), float(t[i]), float(y[i]), threshold)
    return None


def first_falling_crossing(t: np.ndarray, y: np.ndarray, i_peak: int, threshold: float) -> float | None:
    if y[-1] >= threshold:
        return None

    for i in range(i_peak + 1, len(y)):
        if y[i - 1] >= threshold > y[i]:
            return interpolate_time(float(t[i - 1]), float(y[i - 1]), float(t[i]), float(y[i]), threshold)
    return None


def last_falling_crossing(t: np.ndarray, y: np.ndarray, i_peak: int, threshold: float) -> float | None:
    if y[-1] >= threshold:
        return None

    crossing = None
    for i in range(i_peak + 1, len(y)):
        if y[i - 1] >= threshold > y[i]:
            crossing = interpolate_time(float(t[i - 1]), float(y[i - 1]), float(t[i]), float(y[i]), threshold)
    return crossing


def fmt(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return f"{value:.16g}"


def percentile(values: list[float], q: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None

    position = (len(clean) - 1) * q / 100.0
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return clean[lo]

    return clean[lo] * (hi - position) + clean[hi] * (position - lo)


def analyze_file(path: Path) -> dict[str, object]:
    with h5py.File(path, "r") as handle:
        if "t" not in handle or "Q" not in handle:
            raise KeyError("missing required datasets: t and/or Q")
        t = np.asarray(handle["t"][()], dtype=float)
        q = np.asarray(handle["Q"][()], dtype=float)

    if t.size != q.size:
        raise ValueError(f"t/Q length mismatch: {t.size} vs {q.size}")
    if t.size < 2:
        raise ValueError("not enough Q(t) samples")

    q0 = float(q[0])
    qplus = np.maximum(q - q0, 0.0)
    i_peak = int(np.argmax(qplus))
    qplus_max = float(qplus[i_peak])
    q_raw_max = float(np.max(q))
    q_raw_min = float(np.min(q))
    t_qmax = float(t[i_peak])

    result: dict[str, object] = {
        "case": case_name_from_file(path),
        "file": str(path),
        "n_points": int(t.size),
        "t_start_s": float(t[0]),
        "t_end_s": float(t[-1]),
        "t_start_ms": float(t[0] * 1.0e3),
        "t_end_ms": float(t[-1] * 1.0e3),
        "Q0": q0,
        "Q_raw_min": q_raw_min,
        "Q_raw_max": q_raw_max,
        "Qplus_max": qplus_max,
        "index_Qmax": i_peak,
        "t_Qmax_s": t_qmax,
        "t_Qmax_ms": t_qmax * 1.0e3,
    }

    if qplus_max <= 0.0:
        result["status"] = "no_positive_Qplus"
        return result

    crossings: dict[float, tuple[float | None, float | None]] = {}
    for fraction in THRESHOLDS:
        threshold = fraction * qplus_max
        rise = first_rising_crossing(t, qplus, i_peak, threshold)
        if math.isclose(fraction, WINDOW_LOW_THRESHOLD):
            fall = last_falling_crossing(t, qplus, i_peak, threshold)
        else:
            fall = first_falling_crossing(t, qplus, i_peak, threshold)
        crossings[fraction] = (rise, fall)

        label = f"{fraction:g}"
        result[f"t_Q{label}_rise_s"] = rise
        result[f"t_Q{label}_fall_s"] = fall
        result[f"t_Q{label}_rise_ms"] = None if rise is None else rise * 1.0e3
        result[f"t_Q{label}_fall_ms"] = None if fall is None else fall * 1.0e3
        result[f"width_Q{label}_s"] = None if rise is None or fall is None else fall - rise
        result[f"width_Q{label}_ms"] = None if rise is None or fall is None else (fall - rise) * 1.0e3

    rise10 = crossings.get(0.10, (None, None))[0]
    rise90 = crossings.get(0.90, (None, None))[0]
    fall90 = crossings.get(0.90, (None, None))[1]
    fall10 = crossings.get(0.10, (None, None))[1]
    width50 = result.get("width_Q0.5_s")

    result["rise_10_90_s"] = None if rise10 is None or rise90 is None else rise90 - rise10
    result["rise_10_90_ms"] = None if result["rise_10_90_s"] is None else result["rise_10_90_s"] * 1.0e3
    result["fall_90_10_s"] = None if fall90 is None or fall10 is None else fall10 - fall90
    result["fall_90_10_ms"] = None if result["fall_90_10_s"] is None else result["fall_90_10_s"] * 1.0e3

    if isinstance(width50, float) and width50 > 0.0:
        suggested_dt_raw = width50 / TARGET_POINTS_ACROSS_WIDTH_50
        suggested_dt = suggested_dt_raw
        if MIN_SUGGESTED_PROFILE_DT_S is not None:
            suggested_dt = max(MIN_SUGGESTED_PROFILE_DT_S, suggested_dt)
        if MAX_SUGGESTED_PROFILE_DT_S is not None:
            suggested_dt = min(MAX_SUGGESTED_PROFILE_DT_S, suggested_dt)
    else:
        suggested_dt_raw = None
        suggested_dt = None

    low_rise, low_fall = crossings.get(WINDOW_LOW_THRESHOLD, (None, None))
    if low_rise is None:
        refine_start_ms = max(0.0, t_qmax * 1.0e3 - WINDOW_MARGIN_MS)
    else:
        refine_start_ms = max(0.0, low_rise * 1.0e3 - WINDOW_MARGIN_MS)

    if low_fall is None:
        _, fallback_fall = crossings.get(REFINE_END_FALLBACK_THRESHOLD, (None, None))
        if fallback_fall is None:
            refine_end_ms = t_qmax * 1.0e3 + WINDOW_MARGIN_MS
            refine_end_source = "t_Qmax"
        else:
            refine_end_ms = fallback_fall * 1.0e3 + WINDOW_MARGIN_MS
            refine_end_source = f"t_Q{REFINE_END_FALLBACK_THRESHOLD:g}_fall"
    else:
        refine_end_ms = low_fall * 1.0e3 + WINDOW_MARGIN_MS
        refine_end_source = f"t_Q{WINDOW_LOW_THRESHOLD:g}_fall"

    result["suggested_refine_start_ms"] = refine_start_ms
    result["suggested_refine_end_ms"] = refine_end_ms
    result["suggested_refine_end_source"] = refine_end_source
    refine_duration_ms = refine_end_ms - refine_start_ms
    refine_duration_s = refine_duration_ms * 1.0e-3
    result["suggested_refine_duration_ms"] = refine_duration_ms

    result["suggested_profile_dt_s"] = suggested_dt
    result["suggested_profile_dt_raw_s"] = suggested_dt_raw
    result["suggested_profile_dt_ms"] = None if suggested_dt is None else suggested_dt * 1.0e3
    if suggested_dt is not None and suggested_dt > 0.0:
        result["estimated_refine_profile_count"] = math.floor(refine_duration_s / suggested_dt) + 1
        if isinstance(width50, float) and width50 > 0.0:
            result["estimated_Q50_samples"] = width50 / suggested_dt
    else:
        result["estimated_refine_profile_count"] = None
        result["estimated_Q50_samples"] = None
    result["status"] = "ok"
    return result


def write_metrics(rows: list[dict[str, object]], path: Path) -> None:
    columns = [
        "case",
        "file",
        "status",
        "n_points",
        "t_start_ms",
        "t_end_ms",
        "Q0",
        "Q_raw_min",
        "Q_raw_max",
        "Qplus_max",
        "index_Qmax",
        "t_Qmax_ms",
        "width_Q0.5_ms",
        "width_Q0.1_ms",
        "width_Q0.05_ms",
        "width_Q0.01_ms",
        "rise_10_90_ms",
        "fall_90_10_ms",
        "suggested_profile_dt_raw_s",
        "suggested_profile_dt_s",
        "suggested_profile_dt_ms",
        "estimated_refine_profile_count",
        "estimated_Q50_samples",
        "suggested_refine_start_ms",
        "suggested_refine_end_ms",
        "suggested_refine_end_source",
        "suggested_refine_duration_ms",
        "case_plot",
    ]

    for fraction in THRESHOLDS:
        label = f"{fraction:g}"
        for suffix in ("rise_ms", "fall_ms", "ms"):
            key = f"t_Q{label}_{suffix}" if suffix != "ms" else f"width_Q{label}_ms"
            if key not in columns:
                columns.append(key)

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([fmt(row.get(column)) for column in columns])


def write_percentiles(rows: list[dict[str, object]], path: Path) -> None:
    metrics = [
        "t_Qmax_ms",
        "width_Q0.5_ms",
        "width_Q0.1_ms",
        "width_Q0.05_ms",
        "width_Q0.01_ms",
        "rise_10_90_ms",
        "fall_90_10_ms",
        "suggested_profile_dt_raw_s",
        "suggested_profile_dt_s",
        "estimated_refine_profile_count",
        "estimated_Q50_samples",
        "suggested_refine_duration_ms",
    ]
    percentiles = [0, 1, 5, 10, 25, 50, 75, 90, 95, 99, 100]

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "n"] + [f"p{p}" for p in percentiles])
        for metric in metrics:
            values = []
            for row in rows:
                value = row.get(metric)
                if isinstance(value, (float, int)) and math.isfinite(float(value)):
                    values.append(float(value))

            writer.writerow(
                [metric, len(values)]
                + [fmt(percentile(values, p)) for p in percentiles]
            )


def as_float(row: dict[str, object], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def plot_case_q_peak(path: Path, row: dict[str, object], plot_dir: Path) -> Path | None:
    if row.get("status") != "ok":
        return None

    qplus_max = as_float(row, "Qplus_max")
    if qplus_max is None or qplus_max <= 0.0:
        return None

    with h5py.File(path, "r") as handle:
        t_ms = np.asarray(handle["t"][()], dtype=float) * 1.0e3
        q = np.asarray(handle["Q"][()], dtype=float)

    q0 = float(q[0])
    qplus = np.maximum(q - q0, 0.0)
    y = qplus / qplus_max

    t_qmax_ms = as_float(row, "t_Qmax_ms")
    if t_qmax_ms is None:
        return None

    refine_start_ms = as_float(row, "suggested_refine_start_ms")
    refine_end_ms = as_float(row, "suggested_refine_end_ms")
    refine_duration_ms = as_float(row, "suggested_refine_duration_ms")

    if (
        refine_start_ms is not None
        and refine_end_ms is not None
        and refine_end_ms > refine_start_ms
    ):
        extra = max(
            PLOT_ZOOM_MIN_EXTRA_MS,
            PLOT_ZOOM_EXTRA_FRACTION * (refine_end_ms - refine_start_ms),
        )
        x_min = max(float(t_ms[0]), refine_start_ms - extra)
        x_max = min(float(t_ms[-1]), refine_end_ms + extra)
    else:
        fallback_margin = max(PLOT_ZOOM_MIN_EXTRA_MS, WINDOW_MARGIN_MS)
        x_min = max(float(t_ms[0]), t_qmax_ms - 5.0 * fallback_margin)
        x_max = min(float(t_ms[-1]), t_qmax_ms + 5.0 * fallback_margin)

    if x_max <= x_min:
        fallback_margin = max(PLOT_ZOOM_MIN_EXTRA_MS, WINDOW_MARGIN_MS)
        x_min = max(float(t_ms[0]), t_qmax_ms - fallback_margin)
        x_max = min(float(t_ms[-1]), t_qmax_ms + fallback_margin)

    mask = (t_ms >= x_min) & (t_ms <= x_max)
    if np.count_nonzero(mask) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10.5, 5.8), dpi=CASE_PLOT_DPI)
    if refine_start_ms is not None and refine_end_ms is not None and refine_end_ms > refine_start_ms:
        ax.axvspan(
            refine_start_ms,
            refine_end_ms,
            color="#f2c94c",
            alpha=0.16,
            label="dense refine window",
        )
        ax.axvline(refine_start_ms, color="#b8860b", ls="-.", lw=1.4)
        ax.axvline(refine_end_ms, color="#b8860b", ls="-.", lw=1.4)
        ax.text(
            refine_start_ms,
            1.095,
            "start",
            color="#8a6508",
            ha="right",
            va="top",
            rotation=90,
            fontsize=9,
        )
        ax.text(
            refine_end_ms,
            1.095,
            "end",
            color="#8a6508",
            ha="left",
            va="top",
            rotation=90,
            fontsize=9,
        )

    ax.plot(t_ms[mask], y[mask], color="black", lw=2.0, label="Qplus / Qplus_max")
    ax.scatter([t_qmax_ms], [1.0], s=32, color="black", zorder=5)
    ax.text(t_qmax_ms, 1.035, "Qmax", ha="center", va="bottom", fontsize=9)

    colors = {
        0.5: "#d62728",
        0.1: "#1f77b4",
        0.05: "#2ca02c",
        0.01: "#9467bd",
    }
    fallback_colors = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e"]
    arrow_y_positions = {
        0.5: 0.62,
        0.1: 0.20,
        0.05: 0.145,
        0.01: 0.075,
    }

    for index, level in enumerate(PLOT_WIDTH_LEVELS):
        label = f"{level:g}"
        color = colors.get(level, fallback_colors[index % len(fallback_colors)])
        rise = as_float(row, f"t_Q{label}_rise_ms")
        fall = as_float(row, f"t_Q{label}_fall_ms")
        width = as_float(row, f"width_Q{label}_ms")

        ax.axhline(level, color=color, ls="--", lw=1.2, alpha=0.9)
        ax.text(
            x_min + 0.01 * (x_max - x_min),
            level + 0.012,
            f"{label} Qplus_max",
            color=color,
            fontsize=9,
            va="bottom",
        )

        if rise is None or fall is None or width is None:
            continue

        ax.axvline(rise, color=color, ls=":", lw=1.2, alpha=0.95)
        ax.axvline(fall, color=color, ls=":", lw=1.2, alpha=0.95)
        ax.scatter([rise, fall], [level, level], s=26, color=color, zorder=6)

        arrow_y = arrow_y_positions.get(level, min(1.05, level + 0.08))
        ax.annotate(
            "",
            xy=(rise, arrow_y),
            xytext=(fall, arrow_y),
            arrowprops={"arrowstyle": "<->", "color": color, "lw": 1.8},
        )
        label_y = min(1.08, arrow_y + 0.025)
        ax.text(
            0.5 * (rise + fall),
            label_y,
            f"width_Q{label}_ms = {width:.6g} ms",
            color=color,
            ha="center",
            va="bottom",
            fontsize=9,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.82, "pad": 1.5},
        )

    case = str(row.get("case", path.stem))
    suggested_dt = as_float(row, "suggested_profile_dt_s")
    suggested_dt_text = "" if suggested_dt is None else f"{suggested_dt:.3g} s"
    profile_count = as_float(row, "estimated_refine_profile_count")
    profile_count_text = "" if profile_count is None else f", profiles={profile_count:.0f}"
    if (
        refine_start_ms is not None
        and refine_end_ms is not None
        and refine_duration_ms is not None
    ):
        refine_text = (
            f"refine=[{refine_start_ms:.6g}, {refine_end_ms:.6g}] ms, "
            f"duration={refine_duration_ms:.6g} ms"
        )
    else:
        refine_text = "refine window unavailable"
    ax.set_title(
        f"{case}\n"
        f"t_Qmax={t_qmax_ms:.6g} ms, "
        f"dt={suggested_dt_text}{profile_count_text}\n"
        f"{refine_text}"
    )
    ax.set_xlabel("t [ms]")
    ax.set_ylabel("Qplus / Qplus_max [-]")
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(-0.04, 1.12)
    ax.grid(True, alpha=0.28)
    ax.legend(loc="upper right")
    fig.tight_layout()

    plot_dir.mkdir(parents=True, exist_ok=True)
    plot_path = plot_dir / f"{safe_filename(case)}_Q_peak_widths.{CASE_PLOT_FORMAT}"
    fig.savefig(plot_path)
    plt.close(fig)
    return plot_path


def plot_summary_figures(rows: list[dict[str, object]], output_dir: Path) -> None:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    if not ok_rows:
        return

    def values(metric: str) -> list[float]:
        result = []
        for row in ok_rows:
            value = as_float(row, metric)
            if value is not None and value > 0.0:
                result.append(value)
        return result

    panels = [
        ("width_Q0.5_ms", "FWHM: width_Q0.5_ms", "ms"),
        ("width_Q0.1_ms", "10% peak width: width_Q0.1_ms", "ms"),
        ("suggested_profile_dt_s", "Suggested dense profile dt", "s"),
        ("suggested_refine_duration_ms", "Suggested refine duration", "ms"),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), dpi=160)
    for ax, (metric, title, unit) in zip(axes.ravel(), panels):
        data = values(metric)
        if data:
            ax.hist(data, bins=40, color="#4c78a8", edgecolor="white")
            ax.set_xscale("log")
        ax.set_title(title)
        ax.set_xlabel(unit)
        ax.set_ylabel("case count")
        ax.grid(True, alpha=0.25)

    fig.suptitle("Q-peak metric distributions", fontsize=15)
    fig.tight_layout()
    fig.savefig(output_dir / "Q_peak_metric_summary.png")
    plt.close(fig)

    points = []
    for row in ok_rows:
        tq = as_float(row, "t_Qmax_ms")
        w50 = as_float(row, "width_Q0.5_ms")
        w10 = as_float(row, "width_Q0.1_ms")
        if tq is not None and tq > 0.0 and w50 is not None and w50 > 0.0 and w10 is not None and w10 > 0.0:
            points.append((tq, w50, w10))

    if points:
        t_qmax = [item[0] for item in points]
        width50 = [item[1] for item in points]
        width10 = [item[2] for item in points]
        fig, ax = plt.subplots(figsize=(8.5, 6.0), dpi=160)
        ax.scatter(t_qmax, width10, s=18, alpha=0.65, label="width_Q0.1_ms")
        ax.scatter(t_qmax, width50, s=18, alpha=0.65, label="width_Q0.5_ms")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("t_Qmax [ms]")
        ax.set_ylabel("Q-peak width [ms]")
        ax.set_title("Q-peak width vs. ignition timing")
        ax.grid(True, which="both", alpha=0.25)
        ax.legend()
        fig.tight_layout()
        fig.savefig(output_dir / "Q_peak_width_vs_tQmax.png")
        plt.close(fig)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if MAKE_CASE_PLOTS:
        CASE_PLOT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(INPUT_DIR.glob("*.h5"))
    print(f"Input dir: {INPUT_DIR}")
    print(f"Output dir: {OUTPUT_DIR}")
    print(f"H5 files: {len(files)}")

    rows: list[dict[str, object]] = []
    failed_rows: list[list[str]] = []
    plot_count = 0

    for index, path in enumerate(files, start=1):
        if index == 1 or index % 20 == 0:
            print(f"[{index}/{len(files)}] {path.name}", flush=True)

        try:
            row = analyze_file(path)
            if MAKE_CASE_PLOTS and (
                MAX_CASE_PLOTS is None or plot_count < MAX_CASE_PLOTS
            ):
                plot_path = plot_case_q_peak(path, row, CASE_PLOT_DIR)
                if plot_path is not None:
                    row["case_plot"] = str(plot_path)
                    plot_count += 1
            rows.append(row)
        except Exception as exc:
            failed_rows.append([path.name, str(path), type(exc).__name__, str(exc)])
            rows.append(
                {
                    "case": case_name_from_file(path),
                    "file": str(path),
                    "status": f"failed:{type(exc).__name__}",
                }
            )

    metrics_path = OUTPUT_DIR / "Q_peak_metrics.csv"
    percentile_path = OUTPUT_DIR / "Q_peak_metric_percentiles.csv"
    failed_path = OUTPUT_DIR / "Q_peak_metric_failed.csv"

    write_metrics(rows, metrics_path)
    write_percentiles([row for row in rows if row.get("status") == "ok"], percentile_path)
    plot_summary_figures(rows, OUTPUT_DIR)

    with failed_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file_name", "file", "error_type", "message"])
        writer.writerows(failed_rows)

    ok_count = sum(1 for row in rows if row.get("status") == "ok")
    print(f"OK cases: {ok_count}")
    print(f"Failed cases: {len(failed_rows)}")
    print(f"Metrics: {metrics_path}")
    print(f"Percentiles: {percentile_path}")
    if MAKE_CASE_PLOTS:
        print(f"Case plots: {CASE_PLOT_DIR}")
    print(f"Summary plots: {OUTPUT_DIR / 'Q_peak_metric_summary.png'}")
    print(f"Failed: {failed_path}")


if __name__ == "__main__":
    main()
