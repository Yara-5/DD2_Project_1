# Project 1, Option 1: From a Boolean Function to an HD Cell

## 1. Group
Hassan Ashraf, Yara Abdelkader, Youssef Ayman Hassan (<student IDs>) · Option 1 · AI used as a development aid (rules in `AGENTS.md`)

## 2. Setup and quick start
Tools: Yosys 0.52, Icarus Verilog 12.0, OpenSTA, Magic 8.3.684, Netgen 1.5.133, ngspice-47
with the BSIM4 stress fix (course image), Python 3.12 with gdsfactory 9.51.0, klayout 0.30.12
and matplotlib 3.11.2 (`requirements.txt`). Magic, Netgen and ngspice run in a Docker image
that the Makefile builds from `tools/Dockerfile` (the student pack's container recipe, with a
switch for the stock image).

### 2.1 What the machine needs
- **Linux** (Ubuntu), or **Windows with WSL 2** (Ubuntu). On Windows, `make` and the Python
  steps run on Windows (Git Bash with GNU make, Python 3.12), and every Linux tool runs in
  WSL through the `LINUX` prefix.
- On Linux (in WSL on Windows): Docker (`docker` on the PATH, the daemon running), Icarus
  Verilog, Yosys and OpenSTA (`sta`), and `python3` (standard library only). On Ubuntu
  26.04 the packages are `iverilog`, `yosys` and `opensta`.
- Python 3.12 where `PYTHON` runs (Windows under WSL, else Linux):
  `pip install -r requirements.txt`. `make check` stops if a pin differs.
- The sky130A PDK at the version the flow was checked with, through ciel (`pip install ciel`):
  `ciel enable --pdk sky130 --pdk-root "$PDK_ROOT" 1689ac3f2dc763876eaf967227c7dfe831b031ae`
  (this gives `$PDK_ROOT/sky130A`).
- The course files: the student pack (its `given/` folder) and Tutorial 3's `student`
  folder (its `tools.sh`, `flow.py`, `verify.py` and `cells.py` are used unchanged). By
  default they sit next to this repository (`../DD2_Project1_student_pack/given`,
  `../student`); set `GIVEN` and `TUT3` otherwise.
- Tutorial 4's SPM files for Table 5's design-level row: `tools/` holds copies of
  `SPM.v`, `CSADD.v`, `TCMP.v`, `synth.ys` and `abc_constr.txt` as the Tutorial 4 handout
  gives them (`set_load 33.44`), and `TUT4` defaults to `tools`.

### 2.2 `local.mk`
Machine-specific values go in `local.mk` (not committed). On Linux most defaults work:

    PDK_ROOT = /home/<you>/work/pdks

On Windows with WSL:

    PYTHON   = python
    LINUX    = wsl -d Ubuntu-26.04 -e
    PDK_ROOT = /home/<you>/work/pdks
    PDK_HOST = //wsl.localhost/Ubuntu-26.04/home/<you>/work/pdks/ciel/sky130/versions/<hash>/sky130A
    export MSYS_NO_PATHCONV = 1

`PDK` is the PDK as Linux sees it and `PDK_HOST` as `PYTHON` sees it; `MSYS_NO_PATHCONV`
stops Git Bash from rewriting Linux paths. `IMAGE` (default `dd2-layout-tools`) names the
course image and `STOCK_IMAGE` (default `dd2-layout-tools-stock`) the stock one. Tutorial 3's
stock image is also called `dd2-layout-tools`: on a machine that has it, set `IMAGE` to
another name first, or `make check` will find a stock ngspice under that name and rebuild
the tag as the course image. `JOBS` and `INNER_JOBS` set how much runs at once (section 3).

### 2.3 Steps

    make setup               # links the PDK into Tutorial 3's folder for its tools.sh
    make docker-image        # the course image: Magic, Netgen, ngspice-47 with the fix, built
                             # from source; the build itself runs the stress test
    make docker-stock-image  # the stock image for Table 5: ngspice-42, no fix (optional:
                             # make char_validate builds it if STOCK_IMAGE is missing)
    make check               # packages, tools, PDK, given/, the stress test, every rule in rules.json
    make char_validate       # Table 5: our characterizer against the shipped HD Liberty
    make all                 # every request in tests/requests/, characterization, views, round trip
    make test                # make all, then every broken case
    make demo IN=<request.json>   # one request, as at the interview, into build/demo/

`make check` builds the course image itself when `IMAGE` does not exist, and rebuilds it
when the stress test finds a stock ngspice in it (exit 1), then tests it again; the log it
keeps, `results/check/ngspice_stress.log`, is always the last run, and `make views` checks
every characterization against its ngspice SHA-256. A rebuilt image has a binary of its own
(this repository's results were made with SHA-256 `51123371…`), so characterize again after
a rebuild rather than mix results. `make char` and `make demo` need `make char_validate` to
have passed.

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

Runs are parallel: `JOBS` (default 4) cells, DRC contexts or LVS cells at once, and
`INNER_JOBS` (default 2) ngspice runs per cell, so at most 8 ngspice runs at once: each needs
about 0.4 GB, and 16 at once swapped out of this machine's 7.7 GB WSL memory (docs/TECHNICAL.md section 2.4 lists the
decisions behind it). Set them in `local.mk`; `JOBS=1 INNER_JOBS=1` runs everything serially.
A per-cell step that fails only because Windows held a file open (Errno 22 or 13) is run again,
up to three tries; any other failure stops the target at once.

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
| `make demo IN=<request.json>` | one request, any path; needs `make check` and a passed `make char_validate` | the whole flow for that cell (make all's steps, characterization, Liberty view, response, round trip) in `build/demo/cells/<name>/` and `build/demo/results/`, named by the request's `name`; `cells/` and `results/` are not touched; a refused request gets a fail response and exits non-zero |

## 5. Layout
- `src/` the code, one script per step, and `rules.json` (the design rules behind every number)
- `tests/requests/` the flow's inputs; `tests/broken/` the broken cases
- `cells/<name>/` every view of one cell (generated by `make`, committed as the data deliverable)
- `results/<check>/` the check reports (generated by `make`, committed)
- `libs/` the merged Liberty library of the generated cells (`make views`); `report/` the report (not written yet)
- `ai/` the AI use and mistakes logs; `docs/interface/` the Appendix A examples

### Layout optimizations (10 November)
1. Folded fingers of one transistor sit side by side and are joined by one poly bar in the
   npc band; the narrower row's gates are spread out under them and run straight into the
   bar, so each input has one gate contact (nor3_stack_1: 15 -> 10 sites, no met1).
2. Policy `folded_finger_nets_on_met1` (rules.json, off by default because handout Tables 1
   and 3 keep met1 for the rails in a single-stage cell): folded fingers' source/drain nets
   on met1.
3. A path that needs met1 climbs to it at its first landing and comes down at its last
   (`met1_route_li1_step_cost`), and may branch from the net's met1 wire: 37-82 % less li1
   in those nets, maj3_1 and xor3_1 4 vias fewer, maj4n_1 10 -> 7 sites.
4. Diffusion contacts move under their vias where the diffusion and li.3 allow
   (`stack_contacts_under_vias`), so licon and mcon stack: 37 of 46 vias now sit on a contact.
5. As many licons in parallel per diffusion contact (signal and supply) as fit after
   routing (`multiple_licons`; new rule `licon_space`, licon.2): in the full run,
   diffusion licons 190 -> 348 (120 of the 190 contacts have 2 or 3), widths unchanged;
   nand2_1 gets HD's 2 and 3 licons per node. mcon arrays (`mcon_arrays`) only along the
   licon column a via is stacked on (11 November; the first version strung mcons along
   met1 wires, where they carry next to no current).
6. Pin access (`pin_access_met1`, 11 November): every input and output pin keeps a met1
   via pad that no other net's met1 comes within met1.2 of, so a router can reach it.
   13 of the 32 pins in the five met1 cells were blocked; now none. Cost: maj3_1 10 -> 12
   sites, maj4n_1 7 -> 10.
7. The router places the diffusion contacts (`negotiated_router`, 10 November). Fixed
   landings left no horizontal li1 track in the PMOS row (0.45 um between the band-side
   and rail-side landings; a track needs 0.51), so some nets could not route on li1 even
   alone. In the li1-only attempts, after the fixed-landing router fails at a width, a
   second router lands each net on its contacts wherever its wire passes (inside the
   contact's diffusion range) and resolves overlaps by negotiated congestion (PathFinder),
   as HD stacks Y and Y_u1 in a21oi_1's PMOS row. Landings are tall or wide (HD's o22ai_1
   uses a wide one to fit Y's contact above the Y_d1 track). a21oi_1 5 -> 4 sites and
   o22ai_1 7 -> 5 (HD's widths); the stack cells nand3 15 -> 10, o21ai 10 -> 8 and nor4 10
   sites, all three without met1 now; the other cells unchanged (docs/TECHNICAL.md 9.5e).
8. Any two adjacent gates on one input share a poly bar and one contact
   (`poly_bars_same_gate`, as HD's maj3_1 and xor2_1), and euler.py also offers the
   placements with the most such neighbours (xor2_1 `B A | A B n1`): xor3_1 16 -> 14 sites,
   still with met1; docs/TECHNICAL.md 8.8.
9. Folded stages: euler.py also offers the cover with no split leaf (each input's
   fingers side by side) when the cheapest one splits a leaf to save a diffusion break,
   and any placement with the same transistors goes to the planner as an alternative:
   a22oi_stack_1 13 sites and 9 met1 vias -> 10 sites on li1 only (docs/TECHNICAL.md 8.7).
   (a21oi_stack_1 and nand2_stack_1 lost their met1 through item 7's wide landings.)
10. A third sizing family, `"sizing": "opt"` (10 November), beside the handout's `hd` and
   `stack` (which keep inner stages x1): the output stage x1, every other stage sized with an
   Elmore delay model fitted to HD's inv_1 (docs/TECHNICAL.md 7.1). `tests/requests/` holds
   three families: hd (19 cells), stack (nor3_stack_1 and seven more) and opt (the seven
   multi-stage functions, `<function>_opt_1`; xor3_opt_1 held out, its planning too slow).
   The model predicted -0.3 % to -19 % worst delay; characterization does not confirm it:
   against the hd versions, maj4 -5.3 %, a21o, mux2, xor2 within 1 %, maj3 +19.8 %, and2
   +34.4 %, with input capacitance up to 24 % lower (docs/TECHNICAL.md 7.1). The model
   carries no input slope between stages; hd stays the better default.

The full run of 10 November (`make check`, `make test`, `make char`, `make test_views`, all
exit 0) has all five in all 20 cells: 130 sites in all (138 before), 37 met1 vias (60),
summed extracted R 10-47 % lower in every cell, C 3.6-17 % higher (nor3_stack_1 -44 %,
maj4n_1 -3 %, the two narrower cells), median delay -3.9 to +0.4 %; docs/TECHNICAL.md sections
8.7, 9.4, 9.5, 9.5a-c, 9.8 and 19 give the details.

## 6. Results
The scripts that `make` runs write these files; they are committed as the data deliverable (handout section 8.2). The last full run (10-11
November 2026, 33 requests in three families: hd 19, stack 8, opt 6): with the networks and
layouts built beforehand, `make drc lvs compare sim model` 32.6 min, `make -o all test` (the
broken cases) 48 s, `make char` 57.1 min, `make views` 16 s, `make roundtrip` 3.3 min and
`make test_views` 1.7 min, all exit 0 (logs in `build/full_nov10e/`, `build/full_nov10f/`;
`make check` was not part of this run). docs/TECHNICAL.md section 19 has the per-cell table.
- `results/pdk_rules/report.json`: all rule values match the installed sky130A.tech; the ngspice-47 stress test passes
- `results/switch_level/`: every synthesized network and every reordered (Euler) network passes (33/33)
- `results/drc/verification.json`: 792 abutment contexts (33 cells x 24) with 0 DRC errors, the broken context caught; layer check passes on all 33 cells and on installed inv_1 and nand2_1, 132 derived broken copies rejected
- `results/lvs/verification.json`: LVS and RC-LVS match for all 33 cells; LVS against the installed HD cell is required and matches for inv_1, nand2_1, nor2_1, nand3_1, nor3_1, a21oi_1, o21ai_1 (W and L included); reported for a21o_1 and mux2_1 (another circuit, as handout section 4 allows)
- `results/hd_comparison/comparison.json` (Table 4): frame, pins and wiring (li1 inside, met1 only for the rails) pass for all nine generated cells with an HD counterpart. Widths: inv_1, nand2_1, nor2_1 3 sites, nand3_1, nor3_1, a21oi_1 4, a21o_1 6, the same as HD; o21ai_1 5 (HD 4), mux2_1 10 (HD 9). Parasitics and timing are reported for all nine; the median timing difference is within 2.9 % for the seven single-stage cells, -3.2 % for mux2_1 and +5.3 % for a21o_1
- `results/transistor_level/`: every DC row and every single-input edge passes for all 33 cells
- `results/model_level/`: Verilog and Liberty function match the request for all 33; broken models rejected
- `results/characterization/validation.json` (Table 5, `make char_validate`): setup exact; median delay/transition error 3.36 % with the fixed ngspice-47 (limit 5 %), 4.83 % with the stock ngspice-42; input capacitance within 5.5 % (limit 10 %); every Liberty file loads in Yosys and OpenSTA; SPM's worst delay 0.6883 ns with the shipped library, 0.689 ns with ours (+0.10 %); the `--index-by-ramp` broken case is flagged (7.46 %)
- `cells/<name>/characterized.json`: all 33 generated cells characterized on HD nand2_1's 7 x 7 table indices
- `results/sizing/nor3.json`: hd sizing (nor3_1, 4 sites) against stack sizing (nor3_stack_1, 10 sites, 12 transistors): rise delay 32-42 % lower, fall delay 14-25 % higher, input capacitance about 2.1 times, 2.5 times the area
- `make views` and `make roundtrip`: all 33 Liberty views pass `liberty_view.py` and the model-level check on the characterized Liberty, all 33 responses written, and `libs/dd2_g5__tt_025C_1v80.lib` holds the 33 cells. The round trip passes for all 33: both of map.sh's ABC scripts map the one-cell design and ABC cec proves it equivalent to the request; our cell is required, and used, for the five functions hd_reduced.lib does not have (f14_1, f15_1, maj4_1, maj4_opt_1, maj4n_1), and ABC also picks it for 15 of the 28 others. `make test_views`: the three broken cases are rejected
- opt against hd sizing, at the nominal point (read from `characterized.json`; docs/TECHNICAL.md 7.1): worst delay maj4 -5.3 %, a21o +0.7 %, mux2 +0.3 %, xor2 +1.0 %, maj3 +19.8 %, and2 +34.4 %

## 7. Tests
`make test` runs the whole flow and then each broken case in `tests/broken/` against its check
(listed in `tests/broken/README.md`); each must be rejected.

## 8. AI use
AGENTS.md · ai/use_log.md · ai/mistakes_log.md · report/ai_report.pdf

## 9. Limitations
- One single-stage cell (maj4n_1) still needs met1, which handout
  Table 3 reserves for the rails; the plan records `met1_in_single_stage_cell`. Several
  cells are wider than HD where the output crosses the npc band through a routing channel
  (a 0.68-0.70 um gap). 70 of the 190 diffusion contacts keep one licon, where wires pass
  too close for a column (HD uses two or three).
- Multi-stage cells take met1 only after every li1-only attempt (all widths, rip-up included)
  has failed (handout Table 1); xor2_1, xor3_1, maj3_1 and maj4_1 still need it. None of them
  has an HD counterpart, so Table 4's wiring row does not apply to them.
- The transistor-level check simulates all of a cell's cases in one ngspice run per job
  (about 30 s per cell instead of about 80 runs of 45 s), with the same cases and verdicts
  (docs/TECHNICAL.md 12.2); `make drc lvs compare sim model` took 32.6 min on 33 cells.
- The opt sizing family is a measured negative result: its Elmore model carries no input
  slope between stages, and only maj4_opt_1 came out faster than its hd version (and2_opt_1
  +34 %; docs/TECHNICAL.md 7.1). xor3_opt_1 is not in the set: it planned too slowly.
- Planning is slow for cells that cannot route on li1, because every li1 attempt is tried
  before met1: policies `rip_up_with_negotiated` (false) and `lone_net_check` (true) made
  the met1 cells plan 2 to 3 times faster on 10 November (maj3_1 about 4 minutes, xor3_1
  still well over 10).
- The negotiated router (`negotiated_router`) routes li1 only: where the gate order makes
  two nets cross (xor2_1: Y's link between the rows lies between A's two gates), no li1
  router can, at any width. Its failed attempts add planning time. o21ai_1 stays one site
  wider than HD, whose 4-site layout puts a poly contact above the npc band and jogs a gate
  between the rows: Table 4's pin check needs input pins inside the band, and our gate
  columns are straight. mux2_1 (10 sites, HD 9) and maj3_1 (12, HD 8, with met1) are still
  wider than HD.
- The 10 November router changes (items 7 and 8 above) are planned for every cell in
  `build/`; the five cells they change (a21oi_1, o22ai_1, nand3_stack_1, o21ai_stack_1,
  nor4_stack_1) passed DRC (120 contexts), the layer check, LVS, RC-LVS and LVS against HD
  a21oi_1 in `build/chk2`; the full run of 10-11 November then checked them at every level.
- Policy `folded_finger_nets_on_met1` (rules.json, off by default) routes folded fingers'
  source/drain nets on met1, against handout Tables 1 and 3; on nor3_stack_1 it lowers the
  signal nets' extracted R by 6.4 % and raises their C by 4.1 % (docs/TECHNICAL.md 9.5b).
- Parallel runs are limited by memory: on this machine (7.7 GB for WSL) `make char` peaked at
  6.9 GB with 8 ngspice runs at once; a machine with less memory sets a lower `INNER_JOBS`
  or `JOBS` in `local.mk`. A Windows file lock (Errno 22 or 13) makes a per-cell step run
  again; in the 10 November run it happened once (`views-nand2_1`, passed on its second try).
- The 11 November changes (mcon arrays only on licon columns, pin access) were checked on
  the five met1 cells in `build/pa` (DRC 120 contexts 0 errors, layer check, LVS, RC-LVS,
  transistor level); the full run of 10-11 November then checked them at every level.
- Pin access costs width where met1 crosses the gate band between input pins (0.48 um
  apart; a met1 wire needs 0.42 um between two access pads): maj3_1 12 sites, maj4n_1 10.
- Summed extracted R is a crude measure where a layout change adds a parallel path: the
  output net's sum rises in a22oi_1 (+18 %) and o22ai_1 (+17 %) though the pin-to-drain
  resistance does not; timing (`make char`) is the measure that counts.
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
