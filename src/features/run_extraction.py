"""Run the clean extraction notebook as Python without saving cell outputs."""
import argparse
import json
from pathlib import Path
from time import perf_counter

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--mode", choices=["pilot", "full"], default="pilot")
parser.add_argument("--mimic-dir", type=Path)
parser.add_argument("--output-dir", type=Path)
parser.add_argument("--lookback-hours", type=int)
parser.add_argument("--pilot-n", type=int)
parser.add_argument("--synthetic", action="store_true")
args = parser.parse_args()
path = Path(__file__).with_name("feature_extraction.ipynb")
notebook = json.loads(path.read_text(encoding="utf-8-sig"))
started = perf_counter()
namespace = {"__name__": "__main__"}
for number, cell in enumerate(notebook["cells"], start=1):
    if cell["cell_type"] != "code":
        continue
    source = cell["source"]
    exec(compile("".join(source), f"{path.name} cell {number}", "exec"), namespace)
    if number == 1:
        namespace.update(MODE=args.mode, USE_SYNTHETIC=args.synthetic, SHOW_STAY_TRACE=False)
        for setting in ("mimic_dir", "output_dir", "lookback_hours", "pilot_n"):
            if (value := getattr(args, setting)) is not None:
                namespace[setting.upper()] = value
print(f"Total elapsed: {perf_counter() - started:.2f} s")
