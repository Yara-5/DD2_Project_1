# DD2 Project 1, Option 1: the targets the overview requires (section 5.1).
#
#   make setup     link the PDK into Tutorial 3's folder (its tools.sh needs it)
#   make check     package pins, tools, PDK, given files, ngspice stress test, rules vs PDK
#   make reference the six reference cells alone: steps 1-7 and Table 4 (handout section 4)
#   make all       make reference, then the whole flow for every cell in CELLS
#   make test      make all, then every broken case in tests/broken/ must be rejected
#   make demo IN=tests/requests/<cell>.json
#   make char_hd   characterize the six installed reference cells (section 5.1)
#   make char_validate  Table 5: char_hd, the full-ramp broken case, the stock build, syntax, SPM
#   make char      after char_validate: installed a21o_1/mux2_1/nor3_1, then the generated cells in
#                  CELLS (section 5); the NOR3 sizing comparison (8.2); Table 4 again with timing
#   make views     after char: each cell's final Liberty view (cell.lib), the model-level check on
#                  it, the response and report (section 3.3); the merged library in libs/
#   make roundtrip after views: section 3.3's round trip with hd_reduced.lib, map.sh's two ABC
#                  scripts and ABC cec
#   make test_views after roundtrip: the broken cases of the three steps above
#
# Steps that need GDSfactory/KLayout run with PYTHON. Magic, Netgen and ngspice run in
# the course container through Tutorial 3's tools.sh, and Icarus on Linux; on a Linux
# machine LINUX is empty, on Windows it is the WSL prefix. Machine-specific values go in
# local.mk (not committed), for example:
#     PYTHON   = python
#     LINUX    = wsl -d Ubuntu-26.04 -e
#     PDK_ROOT = /home/<you>/work/pdks
#     PDK_HOST = //wsl.localhost/Ubuntu-26.04/home/<you>/work/pdks/ciel/sky130/versions/<hash>/sky130A
#     export MSYS_NO_PATHCONV = 1
#     STOCK_IMAGE = <an image with a stock ngspice and the same Magic, for Table 5; Tutorial 3's
#                    dd2-layout-tools has ngspice-42, which the stress test reports as stock>
#     TUT4 = <Tutorial 4's folder on Linux (SPM.v, synth.ys, ...), for Table 5's design level>
#     NETGEN_ENTRYPOINT = <Netgen's path, if the image is not the course image>
-include local.mk

PYTHON   ?= python3
LINUX    ?=
PDK_ROOT ?= $(HOME)/.ciel
PDK      ?= $(PDK_ROOT)/sky130A
PDK_HOST ?= $(PDK)
GIVEN    ?= ../DD2_Project1_student_pack/given
TUT3     ?= ../student
IMAGE    ?= dd2-layout-tools
STOCK_IMAGE ?=
TUT4     ?=
NETGEN_ENTRYPOINT ?= netgen
NETWORK_CELLS ?= $(sort $(basename $(notdir $(wildcard tests/requests/*.json))))
CELLS    ?= $(NETWORK_CELLS)
OBJECTIVE ?= transistors
PREFIX    = dd2_g5__

LPYTHON   = $(LINUX) env LAYOUT_IMAGE=$(IMAGE) NETGEN_ENTRYPOINT=$(NETGEN_ENTRYPOINT) python3
FRAME     = $(GIVEN)/plan_hd.json
HD_GDS    = $(PDK_HOST)/libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds
HD_SPICE  = $(PDK)/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice
HD_SPICE_HOST = $(PDK_HOST)/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice
TECH      = $(PDK)/libs.tech/magic/sky130A.tech
HD_LIB    = $(PDK)/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
HD_LIB_HOST = $(PDK_HOST)/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
HD_GDS_LINUX = $(PDK)/libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds
REFERENCE_CELLS = inv_1 nand2_1 nor2_1 nand3_1 a21oi_1 o21ai_1
# Installed cells also characterized for Table 4's timing (a21o_1, mux2_1) and Table 6 (nor3_1).
EXTRA_REFERENCE_CELLS = nor3_1 a21o_1 mux2_1

# A broken case passes only when its check exits non-zero.
define expect_fail
	@mkdir -p build/broken
	@if $(2) > build/broken/$(1).log 2>&1; then echo "FAIL: broken case $(1) was accepted (build/broken/$(1).log)"; exit 1; \
	 else echo "ok   broken case $(1) rejected"; fi
endef

.PHONY: setup check test all demo clean reference network layout drc lvs compare sim model char_hd char_validate char \
	views roundtrip test_views

setup:            ## Tutorial 3's pdk link, which its tools.sh mounts into the container
	$(LINUX) ln -sfn $(PDK) $(TUT3)/pdk
	@mkdir -p build cells results

check:            ## tool versions, PDK, and the ngspice stress test for Option 1
	@test -f $(FRAME) || (echo "missing $(FRAME): set GIVEN" && exit 1)
	@test -f $(GIVEN)/hd_reduced.lib || (echo "missing $(GIVEN)/hd_reduced.lib" && exit 1)
	@test -f $(TUT3)/verify.py || (echo "missing Tutorial 3 at $(TUT3): set TUT3" && exit 1)
	@test -f $(HD_GDS) || (echo "missing $(HD_GDS): set PDK_HOST" && exit 1)
	$(LINUX) test -f $(TECH)
	$(LINUX) test -e $(TUT3)/pdk
	$(PYTHON) -c "import sys; from importlib.metadata import version as v; bad=[p for p in open('requirements.txt').read().split() if v(p.split('==')[0])!=p.split('==')[1]]; sys.exit('package pins differ: '+' '.join(bad) if bad else 0)"
	$(LINUX) iverilog -V
	$(LINUX) yosys -V
	@mkdir -p results/check
	@$(LINUX) docker run --rm --entrypoint /opt/dd2/stress_test/check_stress.sh $(IMAGE) > results/check/ngspice_stress.log; \
	  status=$$?; cat results/check/ngspice_stress.log; exit $$status
	$(LPYTHON) src/pdk_rules.py --tech $(TECH) --frame $(FRAME) --out results/pdk_rules/report.json

network:          ## steps 1-3: read (reference cells: HD pins, section 4.1), minimize, decompose, size; switch level
	@mkdir -p build
	@for c in $(NETWORK_CELLS); do \
	  { $(PYTHON) src/request_reader.py tests/requests/$$c.json --out cells/$(PREFIX)$$c/request_original.json > /dev/null || \
	    { $(PYTHON) src/write_response.py --cell-dir cells/$(PREFIX)$$c --raw-request tests/requests/$$c.json; exit 1; }; } && \
	  $(PYTHON) src/reference_cell.py cells/$(PREFIX)$$c/request_original.json --hd-liberty $(HD_LIB_HOST) \
	    --hd-spice $(HD_SPICE_HOST) --out cells/$(PREFIX)$$c/request.json --reference-out cells/$(PREFIX)$$c/hd_reference.json && \
	  $(PYTHON) src/synthesize.py --request cells/$(PREFIX)$$c/request.json --frame $(FRAME) \
	    --hd-reference cells/$(PREFIX)$$c/hd_reference.json \
	    --outdir cells/$(PREFIX)$$c --objective $(OBJECTIVE) > build/synthesize_$$c.log && head -1 build/synthesize_$$c.log && \
	  $(PYTHON) src/switch_level_check.py cells/$(PREFIX)$$c/network.json --request cells/$(PREFIX)$$c/request.json \
	    --out results/switch_level/$${c}_network.json || exit 1; done

layout: network   ## steps 4-5: Euler paths, then plan and draw; the reordered network is checked again
	@for c in $(CELLS); do \
	  $(PYTHON) src/euler.py cells/$(PREFIX)$$c/network.json --hd-spice $(HD_SPICE_HOST) --hd-liberty $(HD_LIB_HOST) && \
	  $(PYTHON) src/switch_level_check.py cells/$(PREFIX)$$c/layout_spec.json --request cells/$(PREFIX)$$c/request.json \
	    --out results/switch_level/$$c.json && \
	  $(PYTHON) src/prepare_layout.py cells/$(PREFIX)$$c/layout_spec.json --request cells/$(PREFIX)$$c/request.json \
	    --frame $(FRAME) --outdir cells && \
	  $(PYTHON) src/generate_gds.py cells/$(PREFIX)$$c/layout_plan.json --tutorial-root $(TUT3) \
	    --outdir cells || exit 1; done

drc:              ## step 6: DRC in tapped rows, abutment, layer check
	$(PYTHON) src/verify_drc.py --tutorial-root $(TUT3) --reference-gds $(HD_GDS) --frame $(FRAME) \
	  --image $(IMAGE) --linux "$(LINUX)"

lvs:              ## step 6: LVS, RC-LVS, LVS against the installed HD cell
	$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) --image $(IMAGE)

compare:          ## section 4.1: comparison with the installed HD cells
	$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME)

sim:              ## section 4.2 level 2: transistor-level simulation of the RC netlist
	@for c in $(CELLS); do \
	  $(LPYTHON) src/transistor_level_check.py --pex cells/$(PREFIX)$$c/$(PREFIX)$${c}_pex.spice \
	    --request cells/$(PREFIX)$$c/request.json --models $(TUT3)/pdk/libs.tech/ngspice/sky130.lib.spice \
	    --liberty $(GIVEN)/hd_reduced.lib --ngspice "$(TUT3)/tools.sh ngspice" \
	    --workdir $(TUT3)/build/dd2_sim_$$c --out results/transistor_level/$$c.json || exit 1; done

model:            ## section 4.2 level 3: Verilog model and Liberty function
	@for c in $(CELLS); do \
	  broken=""; for f in tests/broken/$${c}_*.v; do [ -f "$$f" ] && broken="$$broken --broken $$f"; done; \
	  $(LINUX) python3 src/model_level_check.py --request cells/$(PREFIX)$$c/request.json \
	    --outdir cells/$(PREFIX)$$c --report results/model_level/$$c.json $$broken || exit 1; done

# $(call characterize_hd,<image>,<extra flags>,<output folder>[,<cells>]): installed cells,
# by default the six reference cells.
define characterize_hd
	@for c in $(if $(4),$(4),$(REFERENCE_CELLS)); do \
	  $(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(1) \
	    --hd-cell sky130_fd_sc_hd__$$c --hd-gds $(HD_GDS_LINUX) --pdk-spice $(HD_SPICE) $(2) \
	    --out $(3)/$$c || exit 1; done
endef

char_hd:          ## section 5.1: characterize the six installed reference cells
	$(call characterize_hd,$(IMAGE),,results/characterization/hd)

char_validate: char_hd   ## section 5.1, Table 5; fails while a part did not run (STOCK_IMAGE, TUT4, OpenSTA)
	$(call characterize_hd,$(IMAGE),--index-by-ramp,results/characterization/hd_broken)
	$(if $(STOCK_IMAGE),$(call characterize_hd,$(STOCK_IMAGE),,results/characterization/hd_stock))
	@rm -f results/characterization/design_level.json
	$(if $(TUT4),-$(LPYTHON) src/design_level.py --liberty $(HD_LIB) --cells results/characterization/hd --tutorial4 $(TUT4))
	$(LPYTHON) src/validate_characterizer.py --liberty $(HD_LIB) --fixed results/characterization/hd \
	  --broken results/characterization/hd_broken $(if $(STOCK_IMAGE),--stock results/characterization/hd_stock) \
	  --syntax-check --design-level results/characterization/design_level.json \
	  --out results/characterization/validation.json

char: char_validate   ## section 5: characterize the generated cells (only after Table 5 passes)
	$(call characterize_hd,$(IMAGE),,results/characterization/hd_reference,$(EXTRA_REFERENCE_CELLS))
	@for c in $(CELLS); do \
	  $(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(IMAGE) \
	    --cell-dir cells/$(PREFIX)$$c --out cells/$(PREFIX)$$c/characterized || exit 1; done
	$(PYTHON) src/compare_sizing.py --hd cells/$(PREFIX)nor3_1 --stack cells/$(PREFIX)nor3_stack_1 --out results/sizing/nor3.json
	$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME)

views:            ## step 8: final Liberty view, model level on it, response and report; libs/ (after char)
	@for c in $(CELLS); do \
	  broken=""; for f in tests/broken/$${c}_*.v; do [ -f "$$f" ] && broken="$$broken --broken $$f"; done; \
	  $(PYTHON) src/liberty_view.py --cell-dir cells/$(PREFIX)$$c && \
	  $(LINUX) python3 src/model_level_check.py --request cells/$(PREFIX)$$c/request.json \
	    --outdir cells/$(PREFIX)$$c --report results/model_level/$$c.json --liberty cells/$(PREFIX)$$c/cell.lib $$broken && \
	  $(PYTHON) src/write_response.py --cell-dir cells/$(PREFIX)$$c || exit 1; done
	$(PYTHON) src/liberty_view.py --merge $(foreach c,$(CELLS),cells/$(PREFIX)$(c)/cell.lib) --out libs/$(PREFIX)tt_025C_1v80.lib

roundtrip: views  ## section 3.3: each cell inside hd_reduced.lib, map.sh's scripts A and B, ABC cec
	$(LINUX) python3 src/round_trip.py --cell-dir $(foreach c,$(CELLS),cells/$(PREFIX)$(c)) \
	  --reduced $(GIVEN)/hd_reduced.lib --map-script $(GIVEN)/scripts/map.sh

test_views: roundtrip  ## the broken cases of views and roundtrip (they need characterized cells)
	$(call expect_fail,nand2_1_missing_arc,$(PYTHON) src/liberty_view.py --cell-dir cells/$(PREFIX)nand2_1 --liberty tests/broken/nand2_1_missing_arc.lib --no-write --report build/broken/liberty_view.json)
	$(call expect_fail,nand2_1_wrong_function,$(LINUX) python3 src/model_level_check.py --request cells/$(PREFIX)nand2_1/request.json --outdir build/broken/model_lib --report build/broken/model_lib.json --liberty tests/broken/nand2_1_wrong_function.lib)
	$(call expect_fail,f14_1_wrong_function,$(LINUX) python3 src/round_trip.py --cell-dir cells/$(PREFIX)f14_1 --reduced $(GIVEN)/hd_reduced.lib --map-script $(GIVEN)/scripts/map.sh --liberty tests/broken/f14_1_wrong_function.lib --outdir build/broken/round_trip --workdir build/broken/round_trip_work)
	@echo "PASS: every broken case of views and roundtrip was rejected"

# Handout section 4: steps 1 to 7 on the reference cells first; new functions only after the
# Table 4 comparison (DRC with abutment, LVS against HD, frame and pins) passes.
reference:        ## section 4: build and check the six reference cells alone, before anything else
	$(MAKE) network layout NETWORK_CELLS="$(REFERENCE_CELLS)" CELLS="$(REFERENCE_CELLS)"
	@rm -rf build/reference && mkdir -p build/reference && \
	  for c in $(REFERENCE_CELLS); do cp -r cells/$(PREFIX)$$c build/reference/; done
	$(PYTHON) src/verify_drc.py --tutorial-root $(TUT3) --reference-gds $(HD_GDS) --frame $(FRAME) \
	  --image $(IMAGE) --linux "$(LINUX)" --generated build/reference --outdir build/reference_results/drc
	$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) \
	  --image $(IMAGE) --cells build/reference --outdir build/reference_results/lvs
	$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME) --generated build/reference \
	  --outdir build/reference_results/hd_comparison

all: reference network layout drc lvs compare sim model

test: all         ## every broken case must be rejected by its check
	$(call expect_fail,bad_request,$(PYTHON) src/request_reader.py tests/broken/bad_request.json)
	$(call expect_fail,rules_wrong_poly_space,$(LPYTHON) src/pdk_rules.py --rules tests/broken/rules_wrong_poly_space.json --tech $(TECH) --frame $(FRAME) --out build/broken/pdk_rules.json)
	$(call expect_fail,nand2_1_missing_pullup,$(PYTHON) src/switch_level_check.py tests/broken/nand2_1_missing_pullup.json)
	$(call expect_fail,and2_1_network_missing_pullup,$(PYTHON) src/switch_level_check.py tests/broken/and2_1_network_missing_pullup.json)
	@rm -rf build/broken/lvs && mkdir -p build/broken/lvs/$(PREFIX)nand2_1 && \
	  cp cells/$(PREFIX)nand2_1/cell.gds cells/$(PREFIX)nand2_1/input.json build/broken/lvs/$(PREFIX)nand2_1/ && \
	  cp tests/broken/nand2_1_wrong_width.spice build/broken/lvs/$(PREFIX)nand2_1/schematic.spice
	$(call expect_fail,nand2_1_wrong_width,$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) --image $(IMAGE) --cells build/broken/lvs --outdir build/broken/lvs_results)
	@rm -rf build/broken/lvs_map && mkdir -p build/broken/lvs_map/$(PREFIX)a21oi_1 && \
	  cp cells/$(PREFIX)a21oi_1/cell.gds cells/$(PREFIX)a21oi_1/schematic.spice build/broken/lvs_map/$(PREFIX)a21oi_1/ && \
	  $(PYTHON) src/merge_json.py cells/$(PREFIX)a21oi_1/input.json tests/broken/a21oi_1_wrong_pin_map.json build/broken/lvs_map/$(PREFIX)a21oi_1/input.json
	$(call expect_fail,a21oi_1_wrong_pin_map,$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) --image $(IMAGE) --cells build/broken/lvs_map --outdir build/broken/lvs_map_results)
	@rm -rf build/broken/lvs_swap && mkdir -p build/broken/lvs_swap/$(PREFIX)a21oi_1 && \
	  cp cells/$(PREFIX)a21oi_1/cell.gds cells/$(PREFIX)a21oi_1/schematic.spice build/broken/lvs_swap/$(PREFIX)a21oi_1/ && \
	  $(PYTHON) src/merge_json.py cells/$(PREFIX)a21oi_1/input.json tests/broken/a21oi_1_swapped_hd_pins.json build/broken/lvs_swap/$(PREFIX)a21oi_1/input.json
	$(call expect_fail,a21oi_1_swapped_hd_pins,$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) --image $(IMAGE) --cells build/broken/lvs_swap --outdir build/broken/lvs_swap_results)
	$(call expect_fail,inv_1_no_hvtp,$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME) --generated tests/broken/frame --outdir build/broken/frame_results)
	$(call expect_fail,inv_1_pin_off_band,$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME) --generated tests/broken/pins --outdir build/broken/pin_results)
	$(call expect_fail,nand2_1_pex_missing_pullup,$(LPYTHON) src/transistor_level_check.py --pex tests/broken/nand2_1_pex_missing_pullup.spice --request cells/$(PREFIX)nand2_1/request.json --models $(TUT3)/pdk/libs.tech/ngspice/sky130.lib.spice --liberty $(GIVEN)/hd_reduced.lib --ngspice "$(TUT3)/tools.sh ngspice" --workdir $(TUT3)/build/dd2_sim_broken --out build/broken/transistor_level.json)
	@echo "PASS: every broken case was rejected (DRC and layer-check broken cases run inside make drc)"

demo:             ## what the interview runs: make demo IN=tests/requests/<cell>.json
	@test -n "$(IN)" || (echo "usage: make demo IN=tests/requests/<cell>.json" && exit 1)
	$(MAKE) network layout drc lvs compare sim model NETWORK_CELLS=$(basename $(notdir $(IN))) CELLS=$(basename $(notdir $(IN)))

clean:
	rm -rf build
