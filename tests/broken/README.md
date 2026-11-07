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
| `verify_lvs.py` (HD pin map) | `a21oi_1_wrong_pin_map.json` | a21oi_1 with its pins mapped to HD's A1, A2, B1 in the wrong order (A to B1, C to A1): the HD function is not the request's |
| `verify_lvs.py` (HD pin map) | `a21oi_1_swapped_hd_pins.json` | a21oi_1, which now carries HD's own pin names (`reference_cell.py`), with A1 and B1 swapped in its map to HD: the HD function is not the request's |
| `compare_hd.py` (frame) | `frame/dd2_g5__inv_1_no_hvtp/` | the inverter with its hvtp layer removed |
| `compare_hd.py` (pins) | `pins/dd2_g5__inv_1_pin_off_band/` | the inverter with its input pin shape and label moved 0.6 um up, out of the npc band (Table 4 "Pins") |
| `transistor_level_check.py` | `nand2_1_pex_missing_pullup.spice` | RC-extracted NAND2 with the A pull-up deleted |
| `model_level_check.py` | `nand2_1_wrong_row.v` | Verilog NAND2 that outputs 1 for A=1, B=1 |
| `verify_drc.py` (DRC) | derived in the run: `cp1_broken_met1_width` | a 0.05 um met1 stripe in a tapped row |
| `verify_drc.py` (layer check) | derived in the run: `<cell>_missing_{nsdm,psdm,hvtp,npc}.gds` | each required layer removed from each generated cell |
| `model_level_check.py` | derived in the run: the model with row 0 flipped | one wrong truth-table row |
| `validate_characterizer.py` (Table 5) | derived in `make char_validate`: `characterize.py --index-by-ramp` | the six reference cells with every table slew taken as the full 0-100 % ramp instead of the 20-80 % time |

The derived cases are made by the check itself from a cell that passes, so they
follow every new cell automatically; their results are in `results/drc/verification.json`,
`results/model_level/<cell>.json` and `results/characterization/validation.json`.
