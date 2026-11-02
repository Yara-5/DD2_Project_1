"""Read one functional-simulation operating point from the supplied HD tables."""
import hashlib
import math
import re
from pathlib import Path


def group(text, pattern):
    match = re.search(pattern + r"\s*\{", text)
    if not match:
        raise ValueError(f"Missing Liberty group: {pattern}")
    start = match.end()
    depth = 1
    quoted = False
    for index in range(start, len(text)):
        char = text[index]
        if char == '"' and text[index - 1] != "\\":
            quoted = not quoted
        if not quoted:
            depth += (char == "{") - (char == "}")
            if depth == 0:
                return text[start:index]
    raise ValueError("Unclosed Liberty group")


def read_conditions(path, cell, target_slew_ps=100, target_load_ff=10):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if not cell or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cell):
        raise ValueError("--hd-cell must name an installed HD cell")
    # Read only library attributes, never a similarly named cell attribute.
    header = re.split(r"\bcell\s*\(", text, maxsplit=1)[0]

    def number(key):
        match = re.search(r"\b" + key + r"\s*:\s*([-+0-9.eE]+)\s*;", header)
        if not match or not math.isfinite(float(match[1])):
            raise ValueError(f"Missing/invalid HD setting: {key}")
        return float(match[1])

    thresholds = {key: number(key) for key in (
        "slew_lower_threshold_pct_rise", "slew_upper_threshold_pct_rise",
        "slew_lower_threshold_pct_fall", "slew_upper_threshold_pct_fall")}
    low = thresholds["slew_lower_threshold_pct_rise"]
    high = thresholds["slew_upper_threshold_pct_rise"]
    if not 0 <= low < high <= 100:
        raise ValueError("Invalid HD slew thresholds")
    if low != thresholds["slew_lower_threshold_pct_fall"] or high != thresholds["slew_upper_threshold_pct_fall"]:
        raise ValueError("Different rise/fall slew thresholds need separate ramp durations")
    time = re.search(r'\btime_unit\s*:\s*"([0-9.]+)(ns|ps)"', header)
    cap = re.search(r'\bcapacitive_load_unit\s*\(\s*([0-9.]+)\s*,\s*"?(pf|ff)"?\s*\)', header)
    if not time or not cap:
        raise ValueError("Unsupported/missing Liberty time or capacitance units")
    time_ps = float(time[1]) * (1000 if time[2] == "ns" else 1)
    cap_ff = float(cap[1]) * (1000 if cap[2] == "pf" else 1)
    cell_text = group(text, r'\bcell\s*\(\s*"?' + re.escape(cell) + r'"?\s*\)')
    table = group(cell_text, r'\bcell_rise\s*\([^)]*\)')

    def axis(index, scale):
        match = re.search(r'\bindex_' + str(index) + r'\s*\(\s*"([^"]+)"\s*\)', table)
        if not match:
            raise ValueError(f"HD cell_rise table has no index_{index}")
        values = [float(v.strip()) * scale for v in match[1].split(",")]
        if not values or any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Invalid HD timing axis")
        return values

    slew_axis = axis(1, time_ps)
    load_axis = axis(2, cap_ff)
    slew = min(slew_axis, key=lambda v: abs(v - target_slew_ps))
    load = min(load_axis, key=lambda v: abs(v - target_load_ff))
    if number("nom_voltage") != 1.8 or number("nom_temperature") != 25:
        raise ValueError("This flow requires the HD TT 1.8 V, 25 C library")
    return {
        "source": str(path.resolve()), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cell": cell, "table": "first cell_rise timing table",
        "selection": "nearest table point to 100 ps threshold slew and 10 fF",
        "thresholds_percent": thresholds, "threshold_slew_ps": slew,
        "full_ramp_ps": slew * 100 / (high - low), "load_ff": load,
        "slew_axis_ps": slew_axis, "load_axis_ff": load_axis,
    }
