# motec-dyno-match

Find where a dyno run shows up in a MoTeC log by matching the RPM trace.

Given a MoTeC export (car log, with `MotorRPM` or similar) and a dyno export
(with a tach/wheel RPM channel), this tool locates every time interval in
the MoTeC file whose RPM shape matches the dyno trace. No gear ratio or
calibration is required — the empirical ratio is discovered per match and
reported alongside the result.

Comes with a CLI and a small tkinter GUI for batch matching.

## Why

Dynos and cars log separately and start at different clock times. If you've
ever tried to compare data across the two you've done the manual labour of
scrolling through 30+ minutes of MoTeC data looking for the moment the RPM
trace matches what the dyno recorded. This tool does that automatically,
using normalised cross-correlation on the RPM shape.

## Installation

Requires Python 3.9+. Clone and install the one dependency:

```bash
git clone https://github.com/yeohnicholas4-pixel/motec-dyno-match.git
cd motec-dyno-match
pip install -r requirements.txt
```

`tkinter` ships with standard Python installers on Windows and macOS. On
some Linux distros you may need to install it separately (e.g. `sudo apt
install python3-tk` on Debian/Ubuntu).

## Usage

### GUI (recommended for most uses)

```bash
python motec_dyno_match_gui.py
```

- Click **Add…** under "Dyno files" to pick one or more dyno CSVs
- Click **Add…** under "MoTeC files" to pick one or more car logs
- Adjust the threshold if needed (default 0.90)
- Click **Run match** — results appear in a sortable table

Every dyno × MoTeC combination is checked and each match is listed on its
own row. "no match" rows are shown for pairs where no region was above the
threshold.

### CLI

```bash
python motec_dyno_match.py path/to/motec.csv path/to/dyno.csv
```

With options:

```bash
python motec_dyno_match.py motec.csv dyno.csv --threshold 0.85
python motec_dyno_match.py motec.csv dyno.csv --ratio-min 0.8 --ratio-max 5
```

### As a library

```python
from motec_dyno_match import match_runs

hits = match_runs("motec.csv", "dyno.csv", threshold=0.90)
for h in hits:
    print(f"{h.start:.2f}–{h.end:.2f} s  corr={h.corr:.3f}  ratio={h.ratio:.3f}")
```

`match_runs` returns a list of `Match` objects sorted by descending
correlation. Each `Match` has `start`, `end` (MoTeC-log time in seconds),
`corr` (0–1), `ratio` (empirical MotorRPM / dyno-RPM in the window),
`motec_mean_rpm`, and `dyno_mean_rpm`.

## How it works

1. **Load and resample.** Both CSVs are parsed (the MoTeC metadata header
   is auto-detected) and the RPM channel is interpolated to 20 Hz.
2. **Slide and correlate.** The dyno RPM trace is slid across the MoTeC
   trace; at each position the Pearson correlation of the z-scored shapes
   is computed, plus the empirical mean-ratio `MotorRPM / dynoRPM`.
3. **Filter.** Windows where the vehicle isn't running, where RPM is too
   flat (no shape information), or where the ratio is outside a plausible
   range are discarded.
4. **Peak-pick.** Local maxima above the threshold are extracted with a
   15-second suppression zone so that near-duplicate shifts of the same
   underlying match aren't reported 50 times.

The correlation metric is ratio-independent, so the method works regardless
of where in the driveline the dyno's tach is picking up rotation or how the
dyno's rated-ratio parameter is configured.

## Supported columns

The loader auto-detects columns from these candidates (first match wins):

**MoTeC RPM:** `Car.Data.Motor.MotorRPM`, `MotorRPM`, `Engine Speed`,
`EngineSpeed`
**Dyno RPM:** `Tacho [Rat] (rpm)`, `Tacho (rpm)`, `Engine Speed (rpm)`,
`Tailshaft Speed (rpm)`, `Axle Speed (rpm)`
**Time:** `Time`, `time`, `Time (s)`, `Time (sec)`

If your export uses different names, add them to the `*_CANDIDATES` lists
at the top of `motec_dyno_match.py`.

## False-positive check

A generic RPM drop-rise-settle pattern can appear many times in a long
MoTeC log and match a dyno run by coincidence. The tool helps you spot
these two ways:

1. **Correlation.** A real match usually scores 0.99+. A fuzzy match at
   0.90–0.95 should be viewed with suspicion.
2. **Ratio consistency.** Multiple dyno runs from the same session should
   all match at the same `ratio` value. If one candidate has `ratio=1.32`
   and another has `ratio=3.25`, only one of them is the real match.

If you want stricter filtering, raise the threshold (`--threshold 0.98`)
or narrow the ratio range to match your known drivetrain.

## Troubleshooting

**"No match above threshold" but I know there should be one.**
Try lowering the threshold to 0.80 to see the near-misses. If the best hit
is still below ~0.70, your signals may be noisy, the dyno run may actually
not be in the log, or the wrong columns are being picked up.

**"no RPM column found".**
The loader didn't recognise your file's column names. Open the CSV, note
the actual column name, and add it to `MOTEC_RPM_CANDIDATES` or
`DYNO_RPM_CANDIDATES` at the top of `motec_dyno_match.py`.

**Multiple matches with different ratios.**
Only one is real — see "False-positive check" above.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. If you have a dyno or MoTeC export format that
isn't auto-detected, a sample file (or just the header row) is enough to
add support.
