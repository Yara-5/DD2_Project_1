# Instructions for the AI assistant

This file is the assistant's contract for this repository. `CLAUDE.md` imports it, so
Claude Code reads exactly these rules. Change it only when the group agrees, and commit
the change on its own.

## The project
DD2 Project 1, Option 1 (three-student group, cell prefix `dd2_g5__`): a tool that reads a
single-output Boolean function of up to 4 inputs, written as an expression (`!`, `&`, `|`,
`^`, parentheses) or as a truth table in the cell-request format of Appendix A of the
overview, and writes a sky130_fd_sc_hd-compatible cell: a 2.72 um, whole-site-wide layout in
the instructor's `given/plan_hd.json` frame that passes DRC, LVS, RC-LVS, abutment with HD
cells and the implant/npc layer check, plus a schematic, extracted netlists, a Liberty cell,
a Verilog model and a JSON response. The flow follows the handout's steps: read the request,
find the stages, size, order (Euler path), draw with GDSfactory, verify with Magic and
Netgen, simulate (switch, transistor, model level), characterize with the fixed ngspice,
and run the Yosys round trip. Code is Python 3.12 (gdsfactory 9.51.0, klayout 0.30.12,
matplotlib 3.11.2, otherwise the standard library); Magic 8.3.684, Netgen 1.5.133 and
ngspice 47 with the BSIM4 stress fix run from the course `dd2-layout-tools` container
(built from the student pack's `container/`) through Tutorial 3's unchanged `tools.sh` and
`flow.py`; Icarus Verilog, Yosys/ABC and OpenSTA run on Linux.

Current state: steps 1 to 3 work for any request (`synthesize.py`: Quine-McCluskey,
decomposition into stages of at most 3 series transistors, fewest inverters, sizing).
Layout (steps 5 to 7) still starts from a supplied stage spec with Euler paths in
`tests/specs/`, so only inv_1 and nand2_1 reach GDS. Step 4 (Euler-path search),
characterization (section 5) and the round trip (section 3.3) are not written yet. The
README's Limitations section is the up-to-date list.

## How this project is checked
Pass or fail is the exit status of these commands (0 = pass). Each check also writes a JSON
report with a top-level `"passed"` field under `results/`.

| Command | What it runs |
|---|---|
| `make check` | Python package pins; tool versions; the PDK and `given/` files are present; the ngspice stress test inside the course image (`/opt/dd2/stress_test/check_stress.sh`, must PASS); `src/pdk_rules.py`, which checks every value in `src/rules.json` against the installed `sky130A.tech` |
| `make all` | `make network` for every request in `tests/requests/` (`synthesize.py`, then `switch_level_check.py` on the network), then the layout flow for every cell in `CELLS`: `request_reader.py` -> `switch_level_check.py` -> `prepare_layout.py` -> `generate_gds.py` -> `verify_drc.py` (Magic full DRC in tapped rows, 48 abutment contexts, layer check) -> `verify_lvs.py` (extraction, Netgen LVS against the schematic and against the installed HD subcircuit, RC-LVS) -> `compare_hd.py` -> `transistor_level_check.py` -> `model_level_check.py` |
| `make test` | `make all`, then every broken case in `tests/broken/` against its check; each must be rejected (non-zero exit) |
| `make demo IN=<request>` | The flow on one request, as at the interview |

## Rules the assistant follows (overview, section 4.3)
1. Pass or fail comes from the check scripts, never from the assistant's opinion.
2. Every number in a report comes from a file the flow wrote. Design rule values come from the PDK, never from the assistant.
3. Every check has a broken case in tests/broken/ that it must fail on.
4. The assistant never changes the check scripts, the broken cases or the PDK.
5. A check script exits non-zero when it fails **and** when it cannot run (tool missing, file
   missing, simulator error). A check that did not run is reported as not run, never as passed.
6. The expected function in every check comes from the request's truth table, as normalized
   by `src/request_reader.py` (input 0 is the least significant bit of the row index). No check
   infers the function from the cell name or from the number of inputs.
7. Design-rule numbers live only in `src/rules.json`, each with the rule name it comes from;
   `src/pdk_rules.py` (run by `make check`) proves each one against the installed PDK. Frame
   numbers (height, site width, rails, wells, implants, device models, L) come only from
   `given/plan_hd.json`; supply, temperature, slew thresholds and table indices only from the
   HD Liberty. A literal design-rule value in any other script is a bug.
8. Layout is two stages: `prepare_layout.py` decides every coordinate and writes
   `layout_plan.json`; `generate_gds.py` only draws that plan. Drawing never decides geometry.
9. Rule 4 is relaxed only when a group member explicitly asks, in the session, for a check
   script to change. The change may make a check stricter or more general, never weaker; every
   broken case must still be rejected afterwards; and the change is logged in `ai/use_log.md`.
10. Reuse the instructor's code unchanged (Tutorial 3's `cells.py::rect`, `flow.py::run`,
    `verify.py::magic_script`, `lvs`, `collapse_rc`) by importing it, never by copying and editing it.
11. Every result is reproducible: a file under `cells/` or `results/` is written only by a
    script in `src/` that `make` runs. A result with no script that writes it is deleted, not reported.
12. Report faithfully: say which commands were run, which were skipped and why, and quote the
    failing output. "Generated" is not "verified".

## What the assistant must never do
- Weaken a check to make it pass: widen a tolerance, drop a vector, skip a cell, delete or
  edit a broken case, or catch an error and report success.
- Edit the PDK, `given/`, the Tutorial 3 sources, the container recipe or the stress test.
- Type in a design-rule value, a measured number, a Liberty value or a check result by hand.
- Move, delete or re-create a git tag (`cp1`, `cp2`, `final`); force-push; rewrite pushed history.
- Commit or push unless a group member asks; commit files over 50 MB, the PDK, `build/`,
  `local.mk` or the `pdk`/`given` links.
- Put absolute or user-specific paths in committed code; machine paths go in `local.mk`.
- Claim the generated cells "match" HD where the handout only asks for a report (width, gate
  order, parasitics, timing).

## Conventions
- **Folders.** `src/` code only, plus `src/rules.json`. `tests/` the flow's inputs: requests
  (`tests/*.json`), stage specs (`tests/specs/<cell>.json`) and broken cases (`tests/broken/`,
  listed in `tests/broken/README.md`). `cells/<cell name>/` every view of one cell (request,
  plan, schematic, GDS, picture, extracted netlists, Verilog, Liberty). `results/<check>/`
  check reports. `cells/*` and `results/*` are generated, git-ignored, and rebuilt by
  `make all`. `libs/` merged Liberty files. `report/` the report. `ai/` the AI logs.
  `build/` scratch, never committed. Tools that run in the container work inside Tutorial 3's
  `build/` folder, because its `tools.sh` only mounts that folder and the PDK.
- **Code.** Each script is a command-line tool with `argparse`, a `main()` that returns the
  exit status, and a module docstring that says its step of the handout, its inputs and its
  outputs. Standard library plus the three pinned packages. Paths are relative to the repo
  root. Units: um, um2, ns, pF, V. GDS layer pairs are named once per script.
- **Names.** Cells `dd2_g5__<function>_1`; ports in the order inputs, VGND, VNB, VPB, VPWR, Y;
  NMOS `sky130_fd_pr__nfet_01v8`, PMOS `sky130_fd_pr__pfet_01v8_hvt`.
- **Commits.** Imperative summary of at most 72 characters; a body that says what changed, why,
  and which `make` targets were run with what result. A commit the assistant helped with ends
  with the trailer `AI-assisted: <tool> (<model>)`, for example
  `AI-assisted: Claude Code (Claude Opus 5.5)`, and gets a row in `ai/use_log.md`. A mistake
  the assistant made and a check caught gets a row in `ai/mistakes_log.md`.
