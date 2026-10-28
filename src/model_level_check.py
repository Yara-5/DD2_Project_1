
import argparse
import itertools
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)

    n = len(spec["inputs"])

    if n == 1:
        kind, expected_tt = "inv", 0x1
    elif n == 2:
        kind, expected_tt = "nand2", 0x7
    else:
        raise ValueError("Only inv and nand2 are supported.")

    actual_tt = int(spec["truth_table"], 16)
    if actual_tt != expected_tt:
        raise ValueError(
            f"{kind} requires truth_table={hex(expected_tt)}."
        )

    return spec, kind


def truth(kind, vector):
    if kind == "inv":
        return 1 - vector[0]
    return 1 - (vector[0] & vector[1])


def generate_views(spec, kind, outdir):
    outdir.mkdir(parents=True, exist_ok=True)

    inputs = spec["inputs"]
    output = spec["output"]
    name = spec["name"]

    if kind == "inv":
        verilog_expr = f"~{inputs[0]}"
        liberty_expr = f"!{inputs[0]}"
    else:
        verilog_expr = f"~({inputs[0]} & {inputs[1]})"
        liberty_expr = f"!({inputs[0]} & {inputs[1]})"

    # functional Verilog module
    lines = [
        f"module {name} ({', '.join(inputs + [output])});",
    ]
    lines.extend(f"  input {pin};" for pin in inputs)
    lines.append(f"  output {output};")
    lines.append(f"  assign {output} = {verilog_expr};")
    lines.extend(["endmodule", ""])

    (outdir / "cell.v").write_text(
        "\n".join(lines), encoding="utf-8"
    )

    # functional-only Liberty view
    liberty = [
        "library (dd2_functional) {",
        '  time_unit : "1ns";',
        '  voltage_unit : "1V";',
        '  current_unit : "1mA";',
        '  capacitive_load_unit (1, pf);',
        '  nom_process : 1.0;',
        '  nom_temperature : 25.0;',
        '  nom_voltage : 1.8;',
        f"  cell ({name}) {{",
        "    area : 0.0;",
    ]

    for pin in inputs:
        liberty.extend([
            f"    pin ({pin}) {{",
            "      direction : input;",
            "    }",
        ])

    liberty.extend([
        f"    pin ({output}) {{",
        "      direction : output;",
        f'      function : "{liberty_expr}";',
        "    }",
        "  }",
        "}",
        "",
    ])

    (outdir / "cell.lib").write_text(
        "\n".join(liberty), encoding="utf-8"
    )


def check_liberty(spec, kind, lib_path):
    text = Path(lib_path).read_text(encoding="utf-8")
    output = spec["output"]

    # Locate the output pin's function attribute.
    pattern = (
        r"\bpin\s*\(\s*" + re.escape(output)
        + r"\s*\)\s*\{([^}]*)\}"
    )
    match = re.search(pattern, text, re.S)

    if not match:
        raise ValueError(f"Output pin {output} not found in Liberty.")

    function_match = re.search(
        r'\bfunction\s*:\s*"([^"]+)"\s*;',
        match.group(1)
    )
    if not function_match:
        raise ValueError("Output pin has no Liberty function.")

    function = re.sub(r"\s+", "", function_match.group(1))
    inputs = spec["inputs"]
    rows = []

    for vector in itertools.product((0, 1), repeat=len(inputs)):
        values = dict(zip(inputs, vector))

        if len(inputs) == 1 and function == f"!{inputs[0]}":
            actual = 1 - values[inputs[0]]
        elif (
            len(inputs) == 2
            and function == f"!({inputs[0]}&{inputs[1]})"
        ):
            actual = 1 - (
                values[inputs[0]] & values[inputs[1]]
            )
        else:
            raise ValueError(
                f"Unsupported Liberty function: {function}"
            )

        expected = truth(kind, vector)
        rows.append({
            "inputs": values,
            "expected": expected,
            "actual": actual,
            "passed": actual == expected,
        })

    return {
        "passed": all(row["passed"] for row in rows),
        "function": function,
        "rows": rows,
    }


def check_verilog(spec, kind, verilog_path):
    iverilog = shutil.which("iverilog")
    vvp = shutil.which("vvp")

    if not iverilog or not vvp:
        return {
            "passed": False,
            "reason": (
                "Icarus Verilog is unavailable. Install iverilog "
                "and make sure both iverilog and vvp are on PATH."
            ),
            "rows": [],
        }

    inputs = spec["inputs"]
    output = spec["output"]
    name = spec["name"]

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        tb_path = temp_dir / "tb.v"
        exe_path = temp_dir / "simulation.out"

        tb = [
            "module tb;",
            *[f"  reg {p};" for p in inputs],
            f"  wire {output};",
            f"  {name} dut ({', '.join(inputs + [output])});",
            "  initial begin",
        ]

        for vector in itertools.product((0, 1), repeat=len(inputs)):
            key = "".join(map(str, vector))
            assignments = " ".join(
                f"{pin} = 1'b{bit};"
                for pin, bit in zip(inputs, vector)
            )
            tb.append(f"    {assignments}")
            tb.append("    #1;")
            tb.append(
                f'    $display("ROW_{key}=%b", {output});'
            )

        tb.extend(["    $finish;", "  end", "endmodule", ""])
        tb_path.write_text("\n".join(tb), encoding="utf-8")

        compile_result = subprocess.run(
            [
                iverilog, "-g2012", "-o", str(exe_path),
                str(Path(verilog_path).resolve()), str(tb_path),
            ],
            capture_output=True, text=True,
        )

        if compile_result.returncode != 0:
            return {
                "passed": False,
                "compile_error": compile_result.stderr,
                "rows": [],
            }

        simulation = subprocess.run(
            [vvp, str(exe_path)],
            capture_output=True, text=True,
        )

        if simulation.returncode != 0:
            return {
                "passed": False,
                "simulation_error": simulation.stderr,
                "rows": [],
            }

        observed = {}
        for line in simulation.stdout.splitlines():
            match = re.fullmatch(
                r"ROW_([01]+)=([01xXzZ])", line.strip()
            )
            if match:
                observed[tuple(map(int, match.group(1)))] = (
                    match.group(2).lower()
                )

        rows = []
        for vector in itertools.product((0, 1), repeat=len(inputs)):
            actual = observed.get(vector, "missing")
            expected = truth(kind, vector)
            rows.append({
                "inputs": dict(zip(inputs, vector)),
                "expected": expected,
                "actual": actual,
                "passed": actual == str(expected),
            })

        return {
            "passed": all(row["passed"] for row in rows),
            "rows": rows,
            "stdout": simulation.stdout,
        }


def make_broken_verilog(spec, kind, path):
    """Flip the output for the all-zero input vector."""
    inputs = spec["inputs"]
    output = spec["output"]
    name = spec["name"]
    vectors = list(itertools.product((0, 1), repeat=len(inputs)))
    broken_vector = vectors[0]

    lines = [
        f"module {name} ({', '.join(inputs + [output])});",
        *[f"  input {pin};" for pin in inputs],
        f"  output reg {output};",
        "  always @* begin",
        f"    case ({{{', '.join(inputs)}}})",
    ]

    for vector in vectors:
        result = truth(kind, vector)
        if vector == broken_vector:
            result = 1 - result

        pattern = "".join(map(str, vector))
        lines.append(
            f"      {len(inputs)}'b{pattern}: "
            f"{output} = 1'b{result};"
        )

    lines.extend([
        f"      default: {output} = 1'bx;",
        "    endcase",
        "  end",
        "endmodule",
        "",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cell_spec")
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument(
        "--skip-negative-test",
        action="store_true",
        help="Skip testing the deliberately incorrect Verilog model.",
    )
    args = parser.parse_args()

    spec, kind = load_spec(args.cell_spec)
    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    generate_views(spec, kind, outdir)

    verilog_result = check_verilog(spec, kind, outdir / "cell.v")
    liberty_result = check_liberty(spec, kind, outdir / "cell.lib")

    negative_result = None

    if not args.skip_negative_test:
        broken_path = outdir / "cell_broken.v"
        make_broken_verilog(spec, kind, broken_path)
        broken_result = check_verilog(
            spec, kind, broken_path
        )

        # The negative test passes only if the incorrect model is rejected.
        rejected = not broken_result["passed"]
        negative_result = {
            "broken_model_rejected": rejected,
            "passed": rejected,
            "details": broken_result,
        }

    passed = (
        verilog_result["passed"]
        and liberty_result["passed"]
        and (
            negative_result is None
            or negative_result["passed"]
        )
    )

    report = {
        "check": "model_level",
        "cell": spec["name"],
        "passed": passed,
        "generated": ["cell.v", "cell.lib"],
        "verilog": verilog_result,
        "liberty": liberty_result,
        "negative_test": negative_result,
        "liberty_status": "functional_only_uncharacterized",
    }

    report_path = outdir / "model_level_report.json"
    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))

    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

