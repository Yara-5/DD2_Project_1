
import argparse
import itertools
import json
from collections import deque
from pathlib import Path


def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)

    inputs = spec["inputs"]
    truth_table = int(spec["truth_table"], 16)

    if len(inputs) == 1:
        kind, expected_tt = "inv", 0x1
    elif len(inputs) == 2:
        kind, expected_tt = "nand2", 0x7
    else:
        raise ValueError("Only inv and nand2 are supported.")

    if truth_table != expected_tt:
        raise ValueError(
            f"{kind} requires truth_table={hex(expected_tt)}; "
            f"received {hex(truth_table)}."
        )

    if not spec.get("devices") or not spec.get("output"):
        raise ValueError("The specification needs devices and output.")

    for d in spec["devices"]:
        if d["type"].lower() not in ("nmos", "pmos"):
            raise ValueError(f"Invalid device type: {d['type']}")
        if d["gate"] not in inputs:
            raise ValueError(
                f"{d['id']}: gate {d['gate']} is not a primary input."
            )

    return spec, kind


def expected_output(kind, vector):
    if kind == "inv":
        return 1 - vector[0]
    return 1 - (vector[0] & vector[1])


def connected(graph, start, target):
    """True if conducting switches connect two circuit nodes."""
    queue = deque([start])
    visited = {start}

    while queue:
        node = queue.popleft()

        if node == target:
            return True

        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return False


def check(spec, kind):
    inputs = spec["inputs"]
    output = spec["output"]
    rows = []

    for vector in itertools.product((0, 1), repeat=len(inputs)):
        values = dict(zip(inputs, vector))
        graph = {}

        def add_conducting_switch(a, b):
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)

        # MOS source and drain are treated as bidirectional.
        # NMOS conducts when its gate is 1; PMOS when its gate is 0.
        for device in spec["devices"]:
            gate_value = values[device["gate"]]
            device_type = device["type"].lower()

            is_on = (
                gate_value == 1 if device_type == "nmos"
                else gate_value == 0
            )

            if is_on:
                add_conducting_switch(device["source"], device["drain"])

        expected = expected_output(kind, vector)
        expected_rail = "VPWR" if expected == 1 else "VGND"

        rail_short = connected(graph, "VPWR", "VGND")
        output_on_high = connected(graph, output, "VPWR")
        output_on_low = connected(graph, output, "VGND")

        correct_rail = (output_on_high if expected == 1 else output_on_low)

        # A conducting path to both rails is always a failure.
        output_conflict = output_on_high and output_on_low

        passed = (not rail_short and correct_rail and not output_conflict)

        rows.append({
            "inputs": values,
            "expected_output": expected,
            "expected_rail": expected_rail,
            "output_connected_to_VPWR": output_on_high,
            "output_connected_to_VGND": output_on_low,
            "VPWR_to_VGND_path": rail_short,
            "passed": passed
        })

    return {
        "check": "switch_level",
        "cell": spec["name"],
        "passed": all(row["passed"] for row in rows),
        "rows": rows
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cell_spec", help="Path to the cell JSON file")
    parser.add_argument("--out", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    spec, kind = load_spec(args.cell_spec)
    report = check(spec, kind)

    print(json.dumps(report, indent=2))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8"
        )

    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()