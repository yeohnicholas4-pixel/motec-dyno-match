# motec-dyno-match

Find where a dyno run shows up in a MoTeC log by matching the RPM trace.

## Installation

Requires Python 3.9+. Clone and install the one dependency:

```bash
git clone https://github.com/yeohnicholas4-pixel/motec-dyno-match.git
cd motec-dyno-match
pip install -r requirements.txt
```
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

## Verification
Correlation should score 0.99+, anything less probably just coincidence
Ratio should be consistent across the runs - if it is different to rest
of the data with a high correlation score it is likely false



