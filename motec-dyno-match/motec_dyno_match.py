#!/usr/bin/env python3
"""
Match MoTeC car logs to dyno output logs by finding time intervals in the
MoTeC log whose Motor RPM trace matches the dyno's tach RPM trace.

The matching uses normalized cross-correlation on the RPM shape, which is
drivetrain-ratio independent. No gear ratio or calibration constant is
required; the best-fit ratio is discovered empirically for each candidate
and reported alongside the match.

Usage (CLI):
    python motec_dyno_match.py motec.csv dyno.csv
    python motec_dyno_match.py motec.csv dyno.csv --threshold 0.9

Usage (library):
    from motec_dyno_match import match_runs
    hits = match_runs("motec.csv", "dyno.csv", threshold=0.9)
    for h in hits:
        print(h['start'], h['end'], h['corr'], h['ratio'])
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
#  Configuration                                                              #
# --------------------------------------------------------------------------- #

# Candidate column names for the RPM signal in each file format.
# The loader searches in this order and uses the first one it finds.
MOTEC_RPM_CANDIDATES = [
    "Car.Data.Motor.MotorRPM",
    "MotorRPM",
    "Engine Speed",
    "EngineSpeed",
]
DYNO_RPM_CANDIDATES = [
    "Tacho [Rat] (rpm)",
    "Tacho (rpm)",
    "Engine Speed (rpm)",
    "Tailshaft Speed (rpm)",
    "Axle Speed (rpm)",
]

MOTEC_TIME_CANDIDATES = ["Time", "time", "Time (s)"]
DYNO_TIME_CANDIDATES = ["Time (sec)", "Time", "time", "Time (s)"]

# Physically plausible range for MotorRPM / dyno-RPM.
# The dyno's tach may be at any point in the driveline, so we allow a wide
# window and let the algorithm find the best slope.
RATIO_MIN = 0.5
RATIO_MAX = 10.0

# Minimum RPM to count a sample as "vehicle running" (filter out idle/off).
RUNNING_RPM_THRESHOLD = 200.0

# Fraction of samples in a window that must be "running" for it to be a
# candidate. Prevents matches against long stretches of idle/off.
MIN_RUNNING_FRACTION = 0.80

# Minimum standard deviation of RPM in a candidate window. Flat-line windows
# match anything with a high correlation coefficient and are meaningless.
MIN_WINDOW_STD_RPM = 30.0

# Step (in samples at the resampled rate) between candidate window starts
# during the coarse scan. Smaller = slower but finer.
COARSE_STEP = 4

# After a peak is found, suppress any candidates within this many seconds of
# it before picking the next one (prevents reporting dozens of near-duplicates
# of the same underlying match).
PEAK_SUPPRESS_SECONDS = 15.0

# Resample both signals to this rate (Hz) for comparison.
RESAMPLE_HZ = 20.0


# --------------------------------------------------------------------------- #
#  Data loading                                                               #
# --------------------------------------------------------------------------- #

@dataclass
class Signal:
    """A uniformly-sampled RPM signal with its time vector."""
    t: np.ndarray         # seconds, uniform spacing
    rpm: np.ndarray       # rpm values
    dt: float             # time step
    source_path: str


def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _coerce_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _detect_header_row(path: str) -> int:
    """
    MoTeC CSV exports have ~8 lines of metadata (Device, Date, Time, Driver,
    Vehicle, Track, Type, Comment) followed by a blank line, then the header
    row, then a units row, then data. Dyno exports have the header on line 1.

    Return the 0-indexed line number of the header row.
    """
    with open(path, "r", errors="replace") as f:
        for i, line in enumerate(f):
            if i > 30:
                break
            stripped = line.strip()
            if not stripped:
                continue
            # Heuristic: the header row has many commas and contains "Time"
            # as one of its fields (not as a metadata key like "Time,14:35:10").
            fields = [f.strip() for f in stripped.split(",")]
            if len(fields) >= 5 and "Time" in fields:
                return i
            # Dyno format: starts immediately with "Run Name,Axle Torque..."
            if len(fields) >= 5 and "Time (sec)" in fields:
                return i
    return 0


def _resample(t: np.ndarray, y: np.ndarray, hz: float) -> tuple[np.ndarray, np.ndarray]:
    if len(t) < 2:
        return t, y
    dt = 1.0 / hz
    t_u = np.arange(t[0], t[-1] + 1e-9, dt)
    y_u = np.interp(t_u, t, y)
    return t_u, y_u


def load_rpm_signal(
    path: str,
    rpm_candidates: list[str],
    time_candidates: list[str],
    label: str,
) -> Signal:
    """Load a CSV file and extract the RPM trace, resampled uniformly."""
    header_row = _detect_header_row(path)
    df = pd.read_csv(path, skiprows=header_row, low_memory=False)

    time_col = _find_col(df, time_candidates)
    rpm_col = _find_col(df, rpm_candidates)
    if time_col is None:
        raise ValueError(
            f"{label}: no time column found in {path}. "
            f"Looked for any of: {time_candidates}. "
            f"Available columns: {list(df.columns)[:10]}..."
        )
    if rpm_col is None:
        raise ValueError(
            f"{label}: no RPM column found in {path}. "
            f"Looked for any of: {rpm_candidates}. "
            f"Available columns: {list(df.columns)[:10]}..."
        )

    # Some files have a units row (non-numeric) right under the header.
    # Coerce and drop rows where time can't be parsed.
    df[time_col] = _coerce_numeric(df[time_col])
    df[rpm_col] = _coerce_numeric(df[rpm_col])
    df = df.dropna(subset=[time_col, rpm_col]).reset_index(drop=True)

    if len(df) < 10:
        raise ValueError(f"{label}: fewer than 10 valid rows in {path}")

    t = df[time_col].to_numpy(dtype=float)
    r = df[rpm_col].to_numpy(dtype=float)

    # Ensure monotonically increasing time (drop any out-of-order samples).
    order = np.argsort(t)
    t = t[order]
    r = r[order]
    # Deduplicate timestamps (rare but break interp).
    _, unique_idx = np.unique(t, return_index=True)
    t = t[unique_idx]
    r = r[unique_idx]

    t_u, r_u = _resample(t, r, RESAMPLE_HZ)
    return Signal(t=t_u, rpm=r_u, dt=1.0 / RESAMPLE_HZ, source_path=path)


# --------------------------------------------------------------------------- #
#  Matching                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class Match:
    start: float          # MoTeC log time at start of matched interval (s)
    end: float            # MoTeC log time at end of matched interval (s)
    corr: float           # Pearson correlation between shapes (0..1)
    ratio: float          # Empirical MoTeC_RPM / dyno_RPM in the match
    motec_mean_rpm: float
    dyno_mean_rpm: float


def _zscore(x: np.ndarray) -> np.ndarray:
    m = np.mean(x)
    s = np.std(x)
    if s < 1e-9:
        return np.zeros_like(x)
    return (x - m) / s


def _scan_correlations(
    motec: Signal,
    dyno: Signal,
    ratio_range: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray]:
    """
    Slide the dyno RPM trace over the MoTeC trace; for each start position
    compute the normalized shape correlation and the empirical ratio.

    Returns (correlations, ratios), both indexed by window start index in the
    MoTeC signal (at RESAMPLE_HZ). Invalid windows are marked -inf / nan.
    """
    n_window = len(dyno.rpm)
    n_car = len(motec.rpm)
    n_out = max(0, n_car - n_window + 1)

    corrs = np.full(n_out, -np.inf)
    ratios = np.full(n_out, np.nan)

    d_mean = float(np.mean(dyno.rpm))
    if d_mean < 1.0:
        return corrs, ratios
    d_norm = _zscore(dyno.rpm)

    for start in range(0, n_out, COARSE_STEP):
        w = motec.rpm[start : start + n_window]
        if np.any(np.isnan(w)):
            continue
        running = np.sum(w > RUNNING_RPM_THRESHOLD)
        if running < MIN_RUNNING_FRACTION * n_window:
            continue
        w_std = float(np.std(w))
        if w_std < MIN_WINDOW_STD_RPM:
            continue
        w_mean = float(np.mean(w))
        ratio = w_mean / d_mean
        if not (ratio_range[0] <= ratio <= ratio_range[1]):
            continue
        w_norm = (w - w_mean) / w_std
        corrs[start] = float(np.mean(w_norm * d_norm))
        ratios[start] = ratio

    return corrs, ratios


def _pick_peaks(
    corrs: np.ndarray,
    ratios: np.ndarray,
    motec: Signal,
    dyno: Signal,
    threshold: float,
) -> list[Match]:
    """Greedy peak picking with temporal suppression."""
    suppress_samples = int(round(PEAK_SUPPRESS_SECONDS * RESAMPLE_HZ))
    corrs_work = corrs.copy()
    matches: list[Match] = []

    n_window = len(dyno.rpm)
    d_mean = float(np.mean(dyno.rpm))

    while True:
        best = int(np.argmax(corrs_work))
        if corrs_work[best] < threshold or not np.isfinite(corrs_work[best]):
            break

        start_t = float(motec.t[best])
        end_t = float(motec.t[best + n_window - 1])
        w = motec.rpm[best : best + n_window]
        matches.append(
            Match(
                start=start_t,
                end=end_t,
                corr=float(corrs_work[best]),
                ratio=float(ratios[best]),
                motec_mean_rpm=float(np.mean(w)),
                dyno_mean_rpm=d_mean,
            )
        )

        lo = max(0, best - suppress_samples)
        hi = min(len(corrs_work), best + suppress_samples)
        corrs_work[lo:hi] = -np.inf

    return matches


def match_runs(
    motec_path: str,
    dyno_path: str,
    threshold: float = 0.90,
    ratio_range: tuple[float, float] = (RATIO_MIN, RATIO_MAX),
) -> List[Match]:
    """
    Find every time interval in the MoTeC log whose Motor RPM trace matches
    the shape of the dyno log's tach RPM trace above ``threshold``
    (normalised Pearson correlation, 0..1).

    Parameters
    ----------
    motec_path : str
        Path to the MoTeC CSV (car log, expected to contain MotorRPM).
    dyno_path : str
        Path to the dyno output CSV (expected to contain a tach / speed column).
    threshold : float
        Minimum correlation for a match to be reported. 0.90 is strict but
        avoids false positives on generic RPM bumps; 0.80 catches fuzzier
        matches but may include coincidences.
    ratio_range : (float, float)
        Allowed range of MotorRPM / dyno-RPM. The default (0.5, 10) covers
        any point in a typical driveline.

    Returns
    -------
    list of Match
        Sorted by descending correlation. Empty list if nothing matches.
    """
    motec = load_rpm_signal(motec_path, MOTEC_RPM_CANDIDATES, MOTEC_TIME_CANDIDATES, "MoTeC")
    dyno = load_rpm_signal(dyno_path, DYNO_RPM_CANDIDATES, DYNO_TIME_CANDIDATES, "Dyno")

    if dyno.t[-1] - dyno.t[0] < 2.0:
        raise ValueError("Dyno run is shorter than 2s — too short to match reliably.")
    if motec.t[-1] - motec.t[0] < dyno.t[-1] - dyno.t[0]:
        raise ValueError("MoTeC log is shorter than the dyno run — no match possible.")

    corrs, ratios = _scan_correlations(motec, dyno, ratio_range)
    matches = _pick_peaks(corrs, ratios, motec, dyno, threshold)
    matches.sort(key=lambda m: -m.corr)
    return matches


# --------------------------------------------------------------------------- #
#  CLI                                                                        #
# --------------------------------------------------------------------------- #

def _format_match(m: Match) -> str:
    return (
        f"  {m.start:10.2f} – {m.end:10.2f} s    "
        f"duration {m.end - m.start:6.2f} s    "
        f"corr {m.corr:.4f}    "
        f"ratio {m.ratio:.3f}    "
        f"(motec mean {m.motec_mean_rpm:.0f} rpm, dyno mean {m.dyno_mean_rpm:.0f} rpm)"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Match MoTeC car logs to dyno logs by RPM shape.",
    )
    p.add_argument("motec", help="Path to MoTeC CSV (car log)")
    p.add_argument("dyno", help="Path to dyno output CSV")
    p.add_argument(
        "--threshold", type=float, default=0.90,
        help="Minimum shape correlation (0..1) to report a match. Default 0.90.",
    )
    p.add_argument(
        "--ratio-min", type=float, default=RATIO_MIN,
        help=f"Minimum MotorRPM/dyno-RPM ratio. Default {RATIO_MIN}.",
    )
    p.add_argument(
        "--ratio-max", type=float, default=RATIO_MAX,
        help=f"Maximum MotorRPM/dyno-RPM ratio. Default {RATIO_MAX}.",
    )

    args = p.parse_args(argv)

    try:
        matches = match_runs(
            args.motec,
            args.dyno,
            threshold=args.threshold,
            ratio_range=(args.ratio_min, args.ratio_max),
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not matches:
        print(f"No matches above threshold {args.threshold:.2f}.")
        return 0

    print(f"Found {len(matches)} match(es) above threshold {args.threshold:.2f}:")
    print()
    print("  motec start – motec end    duration          corr      ratio")
    print("  " + "-" * 95)
    for m in matches:
        print(_format_match(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
