
#!/usr/bin/env python3
"""
Checks:
  1. DC operating point for every input combination.
  2. Single-input transitions from every starting input combination.
  3. Final output is within 10% of its expected rail.
  4. If the output should not change, it must not cross VDD/2.

The extracted netlist's subcircuit port order is supplied explicitly.
"""

import argparse
import itertools
import json
import re
import subprocess
import tempfile
from pathlib import Path


VDD = 1.8
VHALF = VDD / 2
LOW_MAX = 0.1 * VDD
HIGH_MIN = 0.9 * VDD

EDGE_TIME_NS = 2.0
SETTLE_TIME_NS = 3.0
STOP_TIME_NS = 5.0
MAX_STEP_PS = 2.0


def parse_args():
    parser = argparse.ArgumentParser(
        description="DC and transient functional verification of an extracted cell."
    )
    parser.add_argument("--pex", required=True, type=Path,
                        help="RC-extracted SPICE netlist")
    parser.add_argument("--models", required=True, type=Path,
                        help="SKY130 model library, e.g. sky130.lib.spice")
    parser.add_argument("--corner", default="tt")
    parser.add_argument("--subckt", required=True,
                        help="Subcircuit name defined in the extracted netlist")
    parser.add_argument("--inputs", required=True,
                        help="Comma-separated ordered input pins, e.g. A or A,B")
    parser.add_argument("--output", default="Y")
    parser.add_argument(
        "--pin-order", required=True,
        help=("Comma-separated subcircuit port order. Allowed names: input pins, "
              "output pin, VPWR, VGND, VPB, VNB. Must match the .subckt declaration.")
    )
    parser.add_argument("--ngspice", default="ngspice",
                        help="Course-patched ngspice executable or wrapper")
    parser.add_argument("--slew-ps", required=True, type=float,
                        help="Full 0-to-100%% input ramp duration in ps")
    parser.add_argument("--load-ff", required=True, type=float,
                        help="Output load in fF")
    parser.add_argument("--out", type=Path, default=Path("transistor_report.json"))
    return parser.parse_args()


def expected_output(bits):
    """Truth tables for the two cells this project is currently targeting."""
    if len(bits) == 1:       # inverter
        return 1 - bits[0]
    if len(bits) == 2:       # two-input NAND
        return 1 - (bits[0] & bits[1])
    raise ValueError("This initial script supports inv and nand2 only.")


def vector_name(bits):
    return "".join(map(str, bits))


def pin_node(pin, inputs, input_values, output, output_node="Y"):
    """Map a logical subcircuit port to a top-level SPICE node."""
    if pin in inputs:
        return f"IN_{pin}"
    if pin == output:
        return output_node
    supplies = {
        "VPWR": "VPWR",
        "VGND": "0",
        "VPB": "VPWR",
        "VNB": "0",
        "VDD": "VPWR",
        "VSS": "0",
    }
    if pin in supplies:
        return supplies[pin]
    raise ValueError(
        f"Unrecognized port {pin!r}. Update pin_node() for your subcircuit."
    )


def make_header(args, nodes):
    return f"""* Generated functional verification deck
.lib "{args.models.resolve().as_posix()}" {args.corner}
.include "{args.pex.resolve().as_posix()}"
.temp 25
V_SUPPLY VPWR 0 {VDD}
"""


def make_instance(args, inputs, input_values):
    ordered_nodes = [
        pin_node(p, inputs, input_values, args.output)
        for p in args.pin_order
    ]
    return "XU " + " ".join(ordered_nodes) + " " + args.subckt


def run_ngspice(args, deck_text, cwd):
    deck = cwd / "check.sp"
    log = cwd / "ngspice.log"
    deck.write_text(deck_text, encoding="utf-8")

    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(
            [args.ngspice, "-b", str(deck)],
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    log_text = log.read_text(encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"ngspice failed (exit {result.returncode}). See {log}.\n"
            + log_text[-2500:]
        )
    if re.search(r"fatal error|no such file|unknown subckt", log_text, re.I):
        raise RuntimeError(f"ngspice reported a simulation error. See {log}.")

    return log_text


def dc_check(args, inputs, bits, workdir):
    """Run a DC operating point with each input held at a logic rail."""
    values = {name: bit for name, bit in zip(inputs, bits)}
    sources = "\n".join(
        f"V_{name} IN_{name} 0 {VDD if values[name] else 0}"
        for name in inputs
    )
    instance = make_instance(args, inputs, values)

    deck = (
        make_header(args, [])
        + sources + "\n"
        + instance + "\n"
        + f"CL Y 0 {args.load_ff}f\n"
        + ".control\nop\n"
        + "echo DC_OUTPUT_START\n"
        + "print v(Y)\n"
        + "echo DC_OUTPUT_END\nquit\n.endc\n.end\n"
    )

    log = run_ngspice(args, deck, workdir)
    match = re.search(
        r"v\(y\)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)",
        log, re.I
    )
    if not match:
        return {
            "inputs": values,
            "expected": expected_output(bits),
            "passed": False,
            "error": "Could not parse v(Y) from ngspice output.",
        }

    voltage = float(match.group(1))
    expected = expected_output(bits)
    passed = voltage <= LOW_MAX if expected == 0 else voltage >= HIGH_MIN

    return {
        "inputs": values,
        "expected": expected,
        "voltage_v": voltage,
        "passed": passed,
    }


def read_waveform(path):
    """
    Read ngspice wrdata output written with:
        set wr_singlescale
        wrdata waveform.dat v(Y)
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        try:
            nums = [float(x) for x in fields]
        except ValueError:
            continue
        if len(nums) >= 2:
            rows.append((nums[0], nums[-1]))
    if not rows:
        raise RuntimeError(f"No numeric waveform samples found in {path}")
    return rows


def transient_check(args, inputs, start_bits, toggle_index, workdir):
    """Toggle exactly one input, starting from a known stable input state."""
    end_bits = list(start_bits)
    end_bits[toggle_index] = 1 - end_bits[toggle_index]

    start_values = dict(zip(inputs, start_bits))
    end_values = dict(zip(inputs, end_bits))
    toggled = inputs[toggle_index]

    # A PULSE source begins at the initial value and makes one transition
    sources = []
    for name in inputs:
        start_v = VDD if start_values[name] else 0

        if name == toggled:
            end_v = VDD if end_values[name] else 0
            sources.append(
                f"V_{name} IN_{name} 0 PULSE("
                f"{start_v} {end_v} {EDGE_TIME_NS}n "
                f"{args.slew_ps}p {args.slew_ps}p 10n 20n)"
            )
        else:
            sources.append(f"V_{name} IN_{name} 0 {start_v}")

    instance = make_instance(args, inputs, start_values)
    waveform = workdir / "waveform.dat"

    deck = (
        make_header(args, [])
        + "\n".join(sources) + "\n"
        + instance + "\n"
        + f"CL Y 0 {args.load_ff}f\n"
        + ".control\n"
        + "set wr_singlescale\n"
        + "set wr_vecnames\n"
        + f"tran {MAX_STEP_PS}p {STOP_TIME_NS}n\n"
        + f'wrdata "{waveform.as_posix()}" v(Y)\n'
        + "quit\n.endc\n.end\n"
    )

    log = run_ngspice(args, deck, workdir)
    if not waveform.exists():
        raise RuntimeError(
            f"ngspice did not create the waveform. Log:\n{log[-2000:]}"
        )

    samples = read_waveform(waveform)
    expected_before = expected_output(start_bits)
    expected_after = expected_output(end_bits)

    # before the edge, then require the output to settle
    pre = [(t, v) for t, v in samples if 1.0 <= t * 1e9 < 1.8]
    post = [(t, v) for t, v in samples if t * 1e9 >= SETTLE_TIME_NS]

    def is_rail(v, bit):
        return v <= LOW_MAX if bit == 0 else v >= HIGH_MIN

    pre_ok = bool(pre) and all(is_rail(v, expected_before) for _, v in pre)
    post_ok = bool(post) and all(is_rail(v, expected_after) for _, v in post)

    # If the Boolean output should not change, it must stay on the same side
    # of VDD/2 throughout the transition, including any charge-sharing glitch.
    no_crossing = True
    if expected_before == expected_after:
        watched = [(t, v) for t, v in samples if t * 1e9 >= 1.8]
        no_crossing = bool(watched) and all(
            (v < VHALF if expected_before == 0 else v > VHALF)
            for _, v in watched
        )

    return {
        "start_inputs": start_values,
        "toggled_input": toggled,
        "end_inputs": end_values,
        "expected_before": expected_before,
        "expected_after": expected_after,
        "pre_edge_valid": pre_ok,
        "settled_by_3ns": post_ok,
        "no_half_supply_crossing_when_output_unchanged": no_crossing,
        "passed": pre_ok and post_ok and no_crossing,
    }


def main():
    args = parse_args()
    inputs = [x.strip() for x in args.inputs.split(",") if x.strip()]
    args.pin_order = [x.strip() for x in args.pin_order.split(",") if x.strip()]

    if len(inputs) not in (1, 2):
        raise SystemExit("Use one input for inv or two inputs for nand2.")
    if args.slew_ps <= 0 or args.load_ff < 0:
        raise SystemExit("--slew-ps must be positive and --load-ff non-negative.")
    if not args.pex.exists():
        raise SystemExit(f"Extracted netlist not found: {args.pex}")
    if not args.models.exists():
        raise SystemExit(f"Model library not found: {args.models}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "cell": args.subckt,
        "corner": args.corner,
        "supply_v": VDD,
        "temperature_c": 25,
        "input_slew_full_ramp_ps": args.slew_ps,
        "output_load_ff": args.load_ff,
        "dc": [],
        "transient": [],
    }

    root = Path("/tmp/dd2_transistor_check_debug")
    if root.exists():
        import shutil
        shutil.rmtree(root)
    root.mkdir(parents=True)
    print(f"Debug files will be preserved in: {root}")

    # DC: every Boolean input vector
    for bits in itertools.product((0, 1), repeat=len(inputs)):
        case_dir = root / ("dc_" + vector_name(bits))
        case_dir.mkdir()
        report["dc"].append(dc_check(args, inputs, bits, case_dir))

    # Transient: from every state, toggle each input
    for bits in itertools.product((0, 1), repeat=len(inputs)):
        for index in range(len(inputs)):
            case_name = f"tr_{vector_name(bits)}_{inputs[index]}"
            case_dir = root / case_name
            case_dir.mkdir()
            report["transient"].append(
                transient_check(args, inputs, bits, index, case_dir)
            )

    report["dc_passed"] = all(x["passed"] for x in report["dc"])
    report["transient_passed"] = all(x["passed"] for x in report["transient"])
    report["passed"] = report["dc_passed"] and report["transient_passed"]

    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "cell": report["cell"],
        "dc_passed": report["dc_passed"],
        "transient_passed": report["transient_passed"],
        "overall_passed": report["passed"],
        "report": str(args.out),
    }, indent=2))

    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()