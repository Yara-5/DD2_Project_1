# Broken cases

Every check has at least one input here that it must reject (AGENTS.md, rule 3).
`make test` runs each one and fails if any is accepted. Never edit these to make a
check pass (AGENTS.md, rule 4).

| Check | Broken case | What is wrong |
|---|---|---|
| `request_reader.py` | `bad_request.json` | expression `!(A & B)` is 0x7, truth table says 0xF |
| `pdk_rules.py` | `rules_wrong_poly_space.json` | poly spacing 0.20 um; the PDK says 0.21 um (poly.2) |
| `switch_level_check.py` | `nand2_1_missing_pullup.json` | NAND2 without the B pull-up: Y floats for A=1, B=0 |
| `switch_level_check.py` (multi-stage) | `and2_1_network_missing_pullup.json` | synthesized AND2 (NAND2 + inverter) without the inverter's pull-up: Y floats for A=B=1 |
| `verify_lvs.py` | `nand2_1_wrong_width.spice` | schematic PMOS XP1 is 0.84 um wide, the layout's is 1.00 um |
| `compare_hd.py` (frame) | `frame/dd2_g5__inv_1_no_hvtp/` | the inverter with its hvtp layer removed |
| `transistor_level_check.py` | `nand2_1_pex_missing_pullup.spice` | RC-extracted NAND2 with the A pull-up deleted |
| `model_level_check.py` | `nand2_1_wrong_row.v` | Verilog NAND2 that outputs 1 for A=1, B=1 |
| `verify_drc.py` (DRC) | derived in the run: `cp1_broken_met1_width` | a 0.05 um met1 stripe in a tapped row |
| `verify_drc.py` (layer check) | derived in the run: `<cell>_missing_{nsdm,psdm,hvtp,npc}.gds` | each required layer removed from each generated cell |
| `model_level_check.py` | derived in the run: the model with row 0 flipped | one wrong truth-table row |

The derived cases are made by the check itself from a cell that passes, so they
follow every new cell automatically; their results are in `results/drc/verification.json`
and `results/model_level/<cell>.json`.
