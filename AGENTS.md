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
`flow.py`; Icarus Verilog, Yosys/ABC and OpenSTA run on Linux. A machine that keeps Tutorial
3's stock image (ngspice-42) under that name builds the course image under another name and
sets `IMAGE` (course image) and `STOCK_IMAGE` (stock image, used only for Table 5's simulator
row) in `local.mk`.

Current state (7 November 2026): milestone 2 is complete. Steps 1 to 7 run from any
request to a checked GDS: `request_reader.py`; `reference_cell.py` (the section 4.1 reference
cells take HD's pin names and transistor sizes from the installed SPICE netlist);
`synthesize.py` (Quine-McCluskey, factored stages of at most 3 series transistors, simple
disjoint decomposition, objective fewest transistors, 'hd' or 'stack' sizing); `euler.py`
(dual-trail Euler paths after Maziasz and Hayes, folding of wide devices into fingers);
`prepare_layout.py` (multi-stage placement, li1 routing, met1 inside a multi-stage cell only
after every li1-only attempt failed, rip-up and reroute); `generate_gds.py`; then the checks
and the comparison with HD (Table 4). The characterizer (`characterize.py`) passes Table 5
(`validate_characterizer.py`, `design_level.py`) and characterizes every generated cell
(`make char`); `compare_sizing.py` compares 'hd' and 'stack' sizing on NOR3.
`liberty_view.py` checks and writes each cell's Liberty view, `write_response.py` the
response and report, and `round_trip.py` runs section 3.3's round trip (`make views
roundtrip test_views`). Not written yet (milestone 3): the design mapped onto HD plus our
cells (step 9), `make all` running characterization onwards, the HD comparison of the
Table 6 cells that have no `hd_reference` yet, and the report. The README's Limitations
section is the up-to-date list.

## How this project is checked
Pass or fail is the exit status of these commands (0 = pass). Each check also writes a JSON
report with a top-level `"passed"` field under `results/`.

| Command | What it runs |
|---|---|
| `make check` | Python package pins; tool versions; the PDK and `given/` files are present; the ngspice stress test inside the course image (`/opt/dd2/stress_test/check_stress.sh`, must PASS); `src/pdk_rules.py`, which checks every value in `src/rules.json` against the installed `sky130A.tech` |
| `make reference` | The six section 4.1 cells alone, before anything else: network, layout, DRC, LVS (against HD required), Table 4 comparison |
| `make all` | `make reference`, then for every request in `tests/requests/`: `request_reader.py` -> `reference_cell.py` -> `synthesize.py` -> `switch_level_check.py` (network) -> `euler.py` -> `switch_level_check.py` (reordered network) -> `prepare_layout.py` -> `generate_gds.py` -> `verify_drc.py` (Magic full DRC in tapped rows, 24 abutment contexts per cell, layer check) -> `verify_lvs.py` (extraction, Netgen LVS against the schematic and against the installed HD subcircuit, RC-LVS) -> `compare_hd.py` -> `transistor_level_check.py` -> `model_level_check.py` |
| `make char_validate` | Table 5: the six installed reference cells characterized with the fixed ngspice (and, with `STOCK_IMAGE`, the stock one), the full-ramp broken case, Yosys and OpenSTA syntax, the SPM design-level run (`TUT4`) |
| `make char` | After `make char_validate`: installed nor3_1, a21o_1, mux2_1 and every generated cell characterized, the NOR3 sizing comparison, `compare_hd.py` with parasitics and timing |
| `make views` | After `make char`: `liberty_view.py` (the Liberty view), `model_level_check.py --liberty`, `write_response.py` (response and report), the merged library in `libs/` |
| `make roundtrip` | After `make views`: `round_trip.py` (each cell inside `hd_reduced.lib`, map.sh's ABC scripts A and B, ABC `cec`) |
| `make test_views` | After `make roundtrip`: the broken cases of the three steps above |
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
  (`tests/requests/<cell>.json`) and broken cases (`tests/broken/`, listed in
  `tests/broken/README.md`). `cells/<cell name>/` every view of one cell (request,
  plan, schematic, GDS, picture, extracted netlists, Verilog, Liberty, response, report).
  `results/<check>/` check reports. `cells/*`, `results/*` and `libs/*` are generated by the
  scripts `make` runs and committed as the data deliverable (handout section 8.2); `make`
  rebuilds them, and they are never edited by hand (rule 11). `libs/` merged Liberty
  files. `report/` the report. `ai/` the AI logs.
  `build/` scratch, never committed. Tools that run in the container work inside Tutorial 3's
  `build/` folder, because its `tools.sh` only mounts that folder and the PDK.
- **Code.** Each script is a command-line tool with `argparse`, a `main()` that returns the
  exit status, and a module docstring that says its step of the handout, its inputs and its
  outputs. Standard library plus the three pinned packages. Paths are relative to the repo
  root. Units: um, um2, ns, pF, V. GDS layer pairs are named once per script.
- **Names.** Cells `dd2_g5__<function>_1`; ports in the order inputs, VGND, VNB, VPB, VPWR,
  output (Y, or the installed cell's name, such as X, for a reference cell); NMOS
  `sky130_fd_pr__nfet_01v8`, PMOS `sky130_fd_pr__pfet_01v8_hvt`.
- **Commits.** Imperative summary of at most 72 characters; a body that says what changed, why,
  and which `make` targets were run with what result. A commit the assistant helped with ends
  with the trailer `AI-assisted: <tool> (<model>)`, for example
  `AI-assisted: Claude Code (Claude Opus 5.5)`, and gets a row in `ai/use_log.md`. A mistake
  the assistant made and a check caught gets a row in `ai/mistakes_log.md`.
