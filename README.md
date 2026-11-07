# Project 1, Option 1: From a Boolean Function to an HD Cell

## 1. Group
<names and student IDs> · Option 1 · AI used as a development aid (rules in `AGENTS.md`)

## 2. Quick start
Tools: Yosys 0.52, Icarus Verilog 12.0, Magic 8.3.684, Netgen 1.5.133, ngspice-47 with the
BSIM4 stress fix (course image, sha256 `51123371…`), Python 3.12 with gdsfactory 9.51.0,
klayout 0.30.12 and matplotlib 3.11.2 (`requirements.txt`).

Put machine-specific paths in `local.mk` (see the top of the `Makefile`; `STOCK_IMAGE` is an
image with a stock ngspice for Table 5, Tutorial 3's `dd2-layout-tools`, and `TUT4` the
Tutorial 4 folder with SPM), then:

    make setup     # links the PDK into Tutorial 3's folder for its tools.sh
    make check     # under a minute
    make all       # about 2 hours for the 20 requests; make demo IN=... for one
    make test      # make all, then every broken case

## 3. What the tool does
It reads a cell request (Appendix A of the overview) and checks that its expression and
truth table agree. For the reference cells (inv_1 ... o21ai_1, nor3_1, a21o_1, mux2_1)
`reference_cell.py` renames the pins to the installed cell's (A1, A2, B1, X), the map found
from the function. `synthesize.py` minimizes the function (Quine-McCluskey), factors it, also
tries disjoint decompositions f = F(h(...), ...), and splits it into static CMOS stages of at
most 3 series transistors with the fewest transistors (handout step 2). It sizes them the HD
way (x1; the installed cell's W and L for the reference cells) or the Tutorial 3 way
(`"sizing": "stack"`: width times stack depth, folded into fingers).
`euler.py` orders each stage's transistors as dual trails (after Maziasz and Hayes, DAC 1991)
and places the stages side by side, abutting on the supplies. `prepare_layout.py` places the
gates in the HD frame of `given/plan_hd.json`, sizes every contact, pitch and diffusion break
from `src/rules.json` (HD-style: one npc band, directional landings, poly contacts beside
their gates, 0.42 um minimum gate pitch) and routes on li1 (met1 only inside multi-stage
cells, or as a recorded last resort), with rip-up and reroute when no net order routes;
`generate_gds.py` draws that plan with GDSfactory. The cell is then checked:
switch level (network and reordered network), DRC in tapped rows with abutment, the
implant/npc layer check, LVS, RC-LVS and LVS against the installed HD cell where one exists,
the frame comparison with HD, a transistor-level simulation of the RC netlist, and the
Verilog/Liberty model check. `characterize.py` (section 5) measures delay, transition and
input capacitance tables on the RC netlist with SA/SB on every transistor, and writes a
Liberty cell; it runs on the installed reference cells (`make char_hd`), is validated against
the shipped Liberty (`make char_validate`, handout Table 5), and then runs on the generated
cells (`make char`).

## 4. Commands
| Command | Input | Output |
|---|---|---|
| `make check` | PDK, `given/`, course image | `results/check/`, `results/pdk_rules/report.json` |
| `make reference` | the six section 4.1 requests | the six cells through DRC, LVS against HD and the Table 4 comparison, alone (`build/reference_results/`); `make all` starts with it |
| `make network` | `tests/requests/*.json` | `cells/<name>/request.json` (`request_original.json`, `hd_reference.json` for reference cells), `network.json`, `network.spice`, `network_schematic.png`, `network_tree.png`, `results/switch_level/<cell>_network.json` |
| `make layout` | `cells/<name>/network.json` | `layout_spec.json` (Euler paths), `results/switch_level/<cell>.json`, `layout_plan.json`, `dimensions.json`, `schematic.spice`, `cell.gds`, `layout_preview.png` |
| `make drc` | `cells/*/cell.gds` | `results/drc/verification.json` and one Magic log per context |
| `make lvs` | `cells/*/cell.gds`, `schematic.spice` | `cells/<name>/<name>_{lvs,cap,pex,rc_collapsed}.spice`, `results/lvs/` |
| `make compare` | `cells/*/cell.gds`, PDK GDS | `results/hd_comparison/comparison.json` (Table 4: frame, pins, wiring must match; width, gate order, parasitics, timing reported), `<cell>_side_by_side.png` |
| `make sim` | `cells/<name>/<name>_pex.spice` | `results/transistor_level/<cell>.json` |
| `make model` | `cells/<name>/request.json` | `cells/<name>/cell.v`, `functional.lib` (function only), `results/model_level/<cell>.json` |
| `make char_hd` | the six installed reference cells, the shipped HD Liberty | `results/characterization/hd/<cell>.{json,lib}` |
| `make char_validate` | `make char_hd`'s results, the full-ramp broken run, the stock run (`STOCK_IMAGE`), Tutorial 4's SPM (`TUT4`) | `results/characterization/validation.json`, `design_level.json` (Table 5) |
| `make char` | `make char_validate` passing; `cells/<name>/cell.gds`, `layout_plan.json` | installed nor3_1, a21o_1, mux2_1 in `results/characterization/hd_reference/`; `cells/<name>/characterized.{json,lib,pex.spice}`; `results/sizing/nor3.json` (section 8.2); the comparison again with timing and parasitics |
| `make views` | `make char`'s results | `cells/<name>/cell.lib` (the checked Liberty view, `results/views/<cell>.json`), the model-level check on it, `cells/<name>/response.json` (Appendix A) and `report.json`; `libs/dd2_g5__tt_025C_1v80.lib` (every cell in CELLS) |
| `make roundtrip` | `make views`'s results, `given/hd_reduced.lib`, `given/scripts/map.sh` | `results/round_trip/<cell>.json`, `summary.json` (section 3.3: each cell inside hd_reduced.lib, mapped with map.sh's scripts A and B, ABC cec) |
| `make test_views` | `make roundtrip` passing | the broken cases of the Liberty view, the model-level check on it and the round trip |
| `make demo IN=tests/requests/<cell>.json` | one request | the whole flow for that cell |

## 5. Layout
- `src/` the code, one script per step, and `rules.json` (the design rules behind every number)
- `tests/requests/` the flow's inputs; `tests/broken/` the broken cases
- `cells/<name>/` every view of one cell (generated by `make`, committed as the data deliverable)
- `results/<check>/` the check reports (generated by `make`, committed)
- `libs/` the merged Liberty library of the generated cells (`make views`); `report/` the report (not written yet)
- `ai/` the AI use and mistakes logs; `docs/interface/` the Appendix A examples

## 6. Results
The scripts that `make` runs write these files; they are committed as the data deliverable (handout section 8.2). The last full run (6-7
November 2026, 20 requests in `tests/requests/`) took 1 h 58 min for `make test` and 53 min
for `make char`; both exited 0. docs/TECHNICAL.md section 19 has the per-cell table.
- `results/pdk_rules/report.json`: all 29 rule values match the installed sky130A.tech; the ngspice-47 stress test passes (ratio 1.035)
- `results/switch_level/`: every synthesized network and every reordered (Euler) network passes
- `results/drc/verification.json`: 480 abutment contexts (20 cells x 24) with 0 DRC errors; layer check passes on all generated cells and on installed inv_1 and nand2_1
- `results/lvs/verification.json`: LVS and RC-LVS match for all 20 cells; LVS against the installed HD cell is required and matches for inv_1, nand2_1, nor2_1, nand3_1, nor3_1, a21oi_1, o21ai_1 (W and L included); reported for a21o_1 and mux2_1 (another circuit, as handout section 4 allows)
- `results/hd_comparison/comparison.json` (Table 4): frame, pins and wiring (li1 inside, met1 only for the rails) pass for all nine cells with an HD counterpart. Widths: inv_1, nand2_1, nor2_1 3 sites, nand3_1, nor3_1 4, a21o_1 6, the same as HD; a21oi_1 and o21ai_1 5 (HD 4), mux2_1 10 (HD 9). Parasitics (R and C per port) and timing are reported for all nine; the median timing difference is within 3.4 % for the seven single-stage cells, -1.0 % for mux2_1 and +10.2 % for a21o_1
- `results/transistor_level/`: every DC row and every single-input edge passes for all 20 cells
- `results/model_level/`: Verilog and Liberty function match the request for all 20; broken models rejected
- `results/characterization/validation.json` (Table 5, `make char_validate`): setup exact; median delay/transition error 3.36 % with the fixed ngspice-47 (limit 5 %), 4.83 % with the stock ngspice-42; input capacitance within 5.5 % (limit 10 %); every Liberty file loads in Yosys and OpenSTA; SPM's worst delay 0.6883 ns with the shipped library, 0.689 ns with ours (+0.10 %); the `--index-by-ramp` broken case is flagged (7.46 %)
- `cells/<name>/characterized.json`: all 20 generated cells characterized on HD nand2_1's 7 x 7 table indices
- `results/sizing/nor3.json`: hd sizing (nor3_1, 4 sites) against stack sizing (nor3_stack_1, 15 sites, 12 transistors): rise delay 22-55 % lower, fall delay 3-48 % higher, 3.75 times the area
- `make views` and `make roundtrip` (7 November, on the cells characterized on 7 November): all 20 Liberty views pass `liberty_view.py` and the model-level check on the characterized Liberty, all 20 responses say "pass", and `libs/dd2_g5__tt_025C_1v80.lib` holds the 20 cells (loads in Yosys and OpenSTA). The round trip passes for all 20 cells: both of map.sh's ABC scripts map the one-cell design and ABC cec proves it equivalent to the request; our cell is required, and used, for the four functions hd_reduced.lib does not have (f14_1, f15_1, maj4_1, maj4n_1), and ABC also picks it for 9 of the 16 functions HD has. `make test_views`: the three broken cases are rejected

## 7. Tests
`make test` runs the whole flow and then each broken case in `tests/broken/` against its check
(listed in `tests/broken/README.md`); each must be rejected.

## 8. AI use
AGENTS.md · ai/use_log.md · ai/mistakes_log.md · report/ai_report.pdf

## 9. Limitations
- Some single-stage cells (maj4n_1, the folded nor3_stack_1) still need met1, which handout
  Table 3 reserves for the rails; the plan records `met1_in_single_stage_cell`. Several
  cells are wider than HD where the output crosses the npc band through a routing channel
  (a 0.68-0.70 um gap). One licon per diffusion contact (HD uses two or three).
- Multi-stage cells take met1 only after every li1-only attempt (all widths, rip-up included)
  has failed (handout Table 1); xor2_1, xor3_1, maj3_1 and maj4_1 still need it. None of them
  has an HD counterpart, so Table 4's wiring row does not apply to them.
- Rip-up and reroute runs only when every net order fails, and takes minutes per cell
  (nor3_stack_1 takes about 20 minutes to plan, maj3_1 about 4).
- `synthesize.py` does not share identical sub-stages; it tries algebraic factorings of
  minimum covers and simple disjoint decompositions only.
- The characterizer gives every generated cell HD nand2_1's table indices; weak cells need a
  longer settling window at the largest load (recorded as `extended_windows_ns`).
- Step 9 (a small design mapped onto HD plus our cells, gate-level simulation, OpenSTA) is
  not done yet; neither is the HD comparison of the Table 6 cells without an `hd_reference`
  (a22oi_1, o22ai_1, and2_1, maj3_1, xor2_1). `make all` stops before characterization;
  `make char views roundtrip test_views` run the rest.
- The given `scripts/map.sh` maps nothing under Yosys 0.52 (its inline ABC script is split at
  the semicolons) and still exits 0; `round_trip.py` reads its commands and scripts and passes
  each script to ABC as a file instead (docs/TECHNICAL.md section 13.8).
- A refused request gets a response with status "fail" and the reader's message; the
  overview's Appendix A text was not at hand, only its example of a passing response.
- `verify_drc.py`'s built-in DRC control (a narrow met1 stripe at a fixed spot of the first
  context) is not caught when the first generated cell has met1 there; with the full set of
  requests it is caught.

## 10. Third-party files
None in the repository. The flow reads, without copying: the course student pack (`given/`,
the container recipe), Tutorial 3's `flow.py`, `verify.py`, `cells.py` and `tools.sh`, and the
SKY130 PDK (Apache 2.0).
