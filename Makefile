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
STOCK_IMAGE ?= dd2-layout-tools-stock
TUT4 ?= tools
NETGEN_ENTRYPOINT ?= netgen
NETWORK_CELLS ?= $(sort $(basename $(notdir $(wildcard tests/requests/*.json))))
CELLS    ?= $(NETWORK_CELLS)
OBJECTIVE ?= transistors
PREFIX    = dd2_g5__
DOCKERFILE := tools/Dockerfile
DOCKER_CONTEXT := tools
STRESS_TEST_DIR := tools/stress_test
# Parallel runs (docs/TECHNICAL.md section 2.4): JOBS cells (or DRC contexts, LVS cells)
# at once; INNER_JOBS ngspice runs per cell in sim and char. JOBS x INNER_JOBS ngspice
# processes is the peak. Memory sets it, not cores: one run loads the sky130 models in
# about 0.4 GB, and on this machine's 7.7 GB WSL 8 runs at once took 40 s while 16 swapped
# (461 s) and were killed (exit 137) in char; the defaults keep it at 8.
JOBS       ?= 4
INNER_JOBS ?= 2
# A sub-make that runs per-cell targets JOBS at a time, each cell's output in one block.
PAR        = $(MAKE) --no-print-directory -j$(JOBS) -Otarget
# A per-cell step is run again (up to 3 tries, 3 s apart) only when it failed on a Windows
# file lock (Errno 22 or 13 in its output: a file under cells/ or results/ held open by
# another program); any other failure, every check failure included, fails at once. The
# step rewrites the same files, so a rerun is safe. Each try is logged in build/logs/.
TRY_START  = mkdir -p build/logs; for try in 1 2 3; do (
TRY_END    = ) > build/logs/$@.log 2>&1 && { cat build/logs/$@.log; exit 0; }; cat build/logs/$@.log; \
	grep -q -E 'Errno (13|22)' build/logs/$@.log || exit 1; \
	echo "$@: Windows file lock (try $$try of 3); running it again in 3 s"; sleep 3; done; exit 1

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

.PHONY: setup docker-image docker-stock-image check test all demo demo-cell clean reference network layout drc lvs compare sim model char_hd char_validate char \
	views roundtrip test_views

setup:            ## Tutorial 3's pdk link, which its tools.sh mounts into the container
	$(LINUX) ln -sfn $(PDK) $(TUT3)/pdk
	@mkdir -p build cells results

docker-image:
	$(LINUX) docker build --build-arg APPLY_STRESS_PATCH=1 \
		-t $(IMAGE) -f $(DOCKERFILE) $(DOCKER_CONTEXT)

# Table 5's stock row is Tutorial 3's stock image, ngspice-42 (AGENTS.md, The project), so the
# stock image is built with ngspice 42 and without the stress fix, not as an unpatched 47.
docker-stock-image:
	$(LINUX) docker build --build-arg APPLY_STRESS_PATCH=0 --build-arg NGSPICE_VERSION=42 \
		-t $(STOCK_IMAGE) -f $(DOCKERFILE) $(DOCKER_CONTEXT)

check:            ## tool versions, PDK, and the ngspice stress test for Option 1
	@test -f $(FRAME) || (echo "missing $(FRAME): set GIVEN" && exit 1)
	@test -f $(GIVEN)/hd_reduced.lib || (echo "missing $(GIVEN)/hd_reduced.lib: check GIVEN" && exit 1)
	@test -f $(TUT3)/verify.py || (echo "missing Tutorial 3 at $(TUT3): set TUT3" && exit 1)
	@test -f $(HD_GDS) || (echo "missing $(HD_GDS): set PDK_HOST" && exit 1)
	@test -f $(HD_LIB_HOST) || (echo "missing $(HD_LIB_HOST): check PDK_HOST" && exit 1)

	$(LINUX) test -f $(TECH)
	$(LINUX) test -e $(TUT3)/pdk

	$(PYTHON) -c "import sys; from importlib.metadata import version as v; bad=[p for p in open('requirements.txt').read().split() if v(p.split('==')[0])!=p.split('==')[1]]; sys.exit('package pins differ: '+' '.join(bad) if bad else 0)"

	@# command is a shell builtin, so it runs inside sh -c: `wsl -e` starts programs only.
	@$(LINUX) sh -c 'command -v iverilog' >/dev/null || (echo "Icarus Verilog is missing" && exit 1)
	@$(LINUX) sh -c 'command -v yosys' >/dev/null || (echo "Yosys is missing" && exit 1)
	@$(LINUX) sh -c 'command -v sta' >/dev/null || (echo "OpenSTA is missing: install OpenSTA" && exit 1)
	@$(LINUX) sh -c 'command -v docker' >/dev/null || (echo "Docker is missing or unavailable" && exit 1)

	$(LINUX) iverilog -V
	$(LINUX) yosys -V
	$(LINUX) sta -version
	@mkdir -p results/check

	@$(LINUX) docker image inspect "$(IMAGE)" >/dev/null 2>&1 || $(MAKE) docker-image

	@# The stress test folder is mounted by its Linux path (asked of the Linux side, so it is
	@# /mnt/c/... under WSL). check_stress.sh exits 1 for a stock ngspice: the image is then
	@# rebuilt as the course image and tested again, and the log always holds the last run,
	@# whose binary SHA-256 liberty_view.py compares with each characterization.
	@mnt=$$($(LINUX) sh -c 'cd $(STRESS_TEST_DIR) && pwd') || exit 2; \
	  stress() { $(LINUX) docker run --rm -v "$$mnt:/opt/dd2/stress_test:ro" \
	    --entrypoint /opt/dd2/stress_test/check_stress.sh $(IMAGE) > results/check/ngspice_stress.log 2>&1; \
	    status=$$?; cat results/check/ngspice_stress.log; }; \
	  stress; \
	  if [ $$status -eq 1 ]; then \
	    echo "$(IMAGE) has a stock ngspice: rebuilding it as the course image, then testing it again"; \
	    $(MAKE) docker-image || exit 1; \
	    stress; \
	  fi; \
	  exit $$status

	$(LPYTHON) src/pdk_rules.py --tech $(TECH) --frame $(FRAME) --out results/pdk_rules/report.json

network:          ## steps 1-3: read (reference cells: HD pins, section 4.1), minimize, decompose, size; switch level
	@mkdir -p build
	@$(PAR) $(addprefix network-,$(NETWORK_CELLS))

network-%:
	@$(TRY_START) c=$*; \
	  { $(PYTHON) src/request_reader.py tests/requests/$$c.json --out cells/$(PREFIX)$$c/request_original.json > /dev/null || \
	    { $(PYTHON) src/write_response.py --cell-dir cells/$(PREFIX)$$c --raw-request tests/requests/$$c.json; exit 1; }; } && \
	  $(PYTHON) src/reference_cell.py cells/$(PREFIX)$$c/request_original.json --hd-liberty $(HD_LIB_HOST) \
	    --hd-spice $(HD_SPICE_HOST) --out cells/$(PREFIX)$$c/request.json --reference-out cells/$(PREFIX)$$c/hd_reference.json && \
	  $(PYTHON) src/synthesize.py --request cells/$(PREFIX)$$c/request.json --frame $(FRAME) \
	    --hd-reference cells/$(PREFIX)$$c/hd_reference.json --hd-liberty $(HD_LIB_HOST) \
	    --outdir cells/$(PREFIX)$$c --objective $(OBJECTIVE) > build/synthesize_$$c.log && head -1 build/synthesize_$$c.log && \
	  $(PYTHON) src/switch_level_check.py cells/$(PREFIX)$$c/network.json --request cells/$(PREFIX)$$c/request.json \
	    --out results/switch_level/$${c}_network.json $(TRY_END)

layout: network   ## steps 4-5: Euler paths, then plan and draw; the reordered network is checked again
	@$(PAR) $(addprefix layout-,$(CELLS))

layout-%:
	@$(TRY_START) c=$*; \
	  $(PYTHON) src/euler.py cells/$(PREFIX)$$c/network.json --hd-spice $(HD_SPICE_HOST) --hd-liberty $(HD_LIB_HOST) && \
	  $(PYTHON) src/switch_level_check.py cells/$(PREFIX)$$c/layout_spec.json --request cells/$(PREFIX)$$c/request.json \
	    --out results/switch_level/$$c.json && \
	  $(PYTHON) src/prepare_layout.py cells/$(PREFIX)$$c/layout_spec.json --request cells/$(PREFIX)$$c/request.json \
	    --frame $(FRAME) --outdir cells && \
	  $(PYTHON) src/generate_gds.py cells/$(PREFIX)$$c/layout_plan.json --tutorial-root $(TUT3) \
	    --outdir cells $(TRY_END)

drc:              ## step 6: DRC in tapped rows, abutment, layer check
	$(PYTHON) src/verify_drc.py --tutorial-root $(TUT3) --reference-gds $(HD_GDS) --frame $(FRAME) \
	  --image $(IMAGE) --linux "$(LINUX)" --jobs $(JOBS) $(if $(CELLS),--cells $(CELLS))

lvs:              ## step 6: LVS, RC-LVS, LVS against the installed HD cell
	$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) --image $(IMAGE) \
	  --jobs $(JOBS)

compare:          ## section 4.1: comparison with the installed HD cells
	$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME)

sim:              ## section 4.2 level 2: transistor-level simulation of the RC netlist
	@$(PAR) $(addprefix sim-,$(CELLS))

sim-%:
	@$(TRY_START) c=$*; \
	  $(LPYTHON) src/transistor_level_check.py --pex cells/$(PREFIX)$$c/$(PREFIX)$${c}_pex.spice \
	    --request cells/$(PREFIX)$$c/request.json --models $(TUT3)/pdk/libs.tech/ngspice/sky130.lib.spice \
	    --liberty $(GIVEN)/hd_reduced.lib --ngspice "$(TUT3)/tools.sh ngspice" --jobs $(INNER_JOBS) \
	    --workdir $(TUT3)/build/dd2_sim_$$c --out results/transistor_level/$$c.json $(TRY_END)

model:            ## section 4.2 level 3: Verilog model and Liberty function
	@$(PAR) $(addprefix model-,$(CELLS))

model-%:
	@$(TRY_START) c=$*; \
	  broken=""; for f in tests/broken/$${c}_*.v; do [ -f "$$f" ] && broken="$$broken --broken $$f"; done; \
	  $(LINUX) python3 src/model_level_check.py --request cells/$(PREFIX)$$c/request.json \
	    --outdir cells/$(PREFIX)$$c --report results/model_level/$$c.json $$broken $(TRY_END)

# $(call characterize_hd,<image>,<extra flags>,<output folder>[,<cells>]): installed cells,
# by default the six reference cells, JOBS at a time. The configurations of one cell (fixed,
# broken, stock) share Tutorial 3's build/dd2_char_<cell> folder, so they never run at once:
# each call finishes before the next starts.
define characterize_hd
	@$(PAR) CH_IMAGE="$(1)" CH_FLAGS="$(2)" CH_OUT="$(3)" $(addprefix charhd-,$(if $(4),$(4),$(REFERENCE_CELLS)))
endef

charhd-%:
	@$(TRY_START) $(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(CH_IMAGE) \
	  --hd-cell sky130_fd_sc_hd__$* --hd-gds $(HD_GDS_LINUX) --pdk-spice $(HD_SPICE) $(CH_FLAGS) \
	  --jobs $(INNER_JOBS) --out $(CH_OUT)/$* $(TRY_END)

char_hd:          ## section 5.1: characterize the six installed reference cells
	$(call characterize_hd,$(IMAGE),,results/characterization/hd)

char_validate: char_hd   ## section 5.1, Table 5; fails while a part did not run (STOCK_IMAGE, TUT4, OpenSTA)
	@$(LINUX) docker image inspect "$(STOCK_IMAGE)" >/dev/null 2>&1 || \
		$(MAKE) docker-stock-image
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
	@$(PAR) $(addprefix char-,$(CELLS))
	$(PYTHON) src/compare_sizing.py --hd cells/$(PREFIX)nor3_1 --stack cells/$(PREFIX)nor3_stack_1 --out results/sizing/nor3.json
	$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME)

char-%:
	@$(TRY_START) $(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(IMAGE) \
	  --jobs $(INNER_JOBS) --cell-dir cells/$(PREFIX)$* --out cells/$(PREFIX)$*/characterized $(TRY_END)

views:            ## step 8: final Liberty view, model level on it, response and report; libs/ (after char)
	@$(PAR) $(addprefix views-,$(CELLS))
	$(PYTHON) src/liberty_view.py --merge $(foreach c,$(CELLS),cells/$(PREFIX)$(c)/cell.lib) --out libs/$(PREFIX)tt_025C_1v80.lib

views-%:
	@$(TRY_START) c=$*; \
	  broken=""; for f in tests/broken/$${c}_*.v; do [ -f "$$f" ] && broken="$$broken --broken $$f"; done; \
	  $(PYTHON) src/liberty_view.py --cell-dir cells/$(PREFIX)$$c && \
	  $(LINUX) python3 src/model_level_check.py --request cells/$(PREFIX)$$c/request.json \
	    --outdir cells/$(PREFIX)$$c --report results/model_level/$$c.json --liberty cells/$(PREFIX)$$c/cell.lib $$broken && \
	  $(PYTHON) src/write_response.py --cell-dir cells/$(PREFIX)$$c $(TRY_END)

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
	  --image $(IMAGE) --linux "$(LINUX)" --generated build/reference --outdir build/reference_results/drc --jobs $(JOBS)
	$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) \
	  --image $(IMAGE) --cells build/reference --outdir build/reference_results/lvs --jobs $(JOBS)
	$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME) --generated build/reference \
	  --outdir build/reference_results/hd_comparison

all: reference network layout drc lvs compare sim model char views roundtrip

test: all test_views         ## every broken case must be rejected by its check
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

# make demo IN=<request.json>: the whole flow on one request, as at the interview. Every file
# goes to build/demo/ (cells/<name>/ and results/<check>/ inside it), so a demo never
# overwrites the committed cells/ and results/. The cell's name is the request's own "name",
# read by request_reader.py, not the file name. Characterization needs Table 5 to have
# passed (results/characterization/validation.json, make char_validate) and make check's
# stress log, as make char and make views do.
DEMO_DIR := build/demo
demo:             ## the flow on one request, as at the interview: make demo IN=<request.json>
	@test -n "$(IN)" || (echo "usage: make demo IN=<request.json>" && exit 1)
	@test -f "$(IN)" || (echo "error: file $(IN) does not exist" && exit 1)
	@$(PYTHON) -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get('passed') else 1)" \
	  results/characterization/validation.json 2>/dev/null || \
	  (echo "Table 5 has not passed (results/characterization/validation.json): run make char_validate first" && exit 1)
	@test -f results/check/ngspice_stress.log || (echo "no results/check/ngspice_stress.log: run make check first" && exit 1)
	@rm -rf build/demo
	@mkdir -p $(DEMO_DIR)/cells $(DEMO_DIR)/results
	@if $(PYTHON) src/request_reader.py "$(IN)" --out $(DEMO_DIR)/request_original.json > /dev/null; then \
	  name=$$($(PYTHON) -c "import json,sys; print(json.load(open(sys.argv[1]))['request']['name'])" $(DEMO_DIR)/request_original.json) && \
	  mkdir -p $(DEMO_DIR)/cells/$$name && cp $(DEMO_DIR)/request_original.json $(DEMO_DIR)/cells/$$name/ && \
	  $(MAKE) --no-print-directory demo-cell DEMO_CELL=$$name; \
	else \
	  mkdir -p $(DEMO_DIR)/cells/refused && cp $(DEMO_DIR)/request_original.json $(DEMO_DIR)/cells/refused/ && \
	  $(PYTHON) src/write_response.py --cell-dir $(DEMO_DIR)/cells/refused --raw-request "$(IN)" \
	    --results $(DEMO_DIR)/results; \
	  echo "The request was refused; the response is $(DEMO_DIR)/cells/refused/response.json"; exit 1; \
	fi

DEMO_C = $(DEMO_DIR)/cells/$(DEMO_CELL)
DEMO_R = $(DEMO_DIR)/results
DEMO_S = $(patsubst $(PREFIX)%,%,$(DEMO_CELL))
demo-cell:        ## (called by make demo) the steps of make all, char and views on DEMO_CELL
	@echo "== 1-3 read, reference, minimize, stages, size: $(DEMO_CELL)"
	$(PYTHON) src/reference_cell.py $(DEMO_C)/request_original.json --hd-liberty $(HD_LIB_HOST) \
	  --hd-spice $(HD_SPICE_HOST) --out $(DEMO_C)/request.json --reference-out $(DEMO_C)/hd_reference.json
	$(PYTHON) src/synthesize.py --request $(DEMO_C)/request.json --frame $(FRAME) \
	  --hd-reference $(DEMO_C)/hd_reference.json --hd-liberty $(HD_LIB_HOST) \
	  --outdir $(DEMO_C) --objective $(OBJECTIVE)
	$(PYTHON) src/switch_level_check.py $(DEMO_C)/network.json --request $(DEMO_C)/request.json \
	  --out $(DEMO_R)/switch_level/$(DEMO_S)_network.json
	@echo "== 4-5 Euler paths, layout plan, GDS"
	$(PYTHON) src/euler.py $(DEMO_C)/network.json --hd-spice $(HD_SPICE_HOST) --hd-liberty $(HD_LIB_HOST)
	$(PYTHON) src/switch_level_check.py $(DEMO_C)/layout_spec.json --request $(DEMO_C)/request.json \
	  --out $(DEMO_R)/switch_level/$(DEMO_S).json
	$(PYTHON) src/prepare_layout.py $(DEMO_C)/layout_spec.json --request $(DEMO_C)/request.json \
	  --frame $(FRAME) --outdir $(DEMO_DIR)/cells
	$(PYTHON) src/generate_gds.py $(DEMO_C)/layout_plan.json --tutorial-root $(TUT3) --outdir $(DEMO_DIR)/cells
	@echo "== 6 DRC in tapped rows, abutment, layer check; LVS, RC-LVS"
	$(PYTHON) src/verify_drc.py --tutorial-root $(TUT3) --reference-gds $(HD_GDS) --frame $(FRAME) \
	  --image $(IMAGE) --linux "$(LINUX)" --jobs $(JOBS) --generated $(DEMO_DIR)/cells --outdir $(DEMO_R)/drc
	$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --hd-liberty $(HD_LIB) \
	  --image $(IMAGE) --jobs $(JOBS) --cells $(DEMO_DIR)/cells --outdir $(DEMO_R)/lvs
	@echo "== 7 transistor level (the extracted RC netlist) and model level"
	$(LPYTHON) src/transistor_level_check.py --pex $(DEMO_C)/$(DEMO_CELL)_pex.spice \
	  --request $(DEMO_C)/request.json --models $(TUT3)/pdk/libs.tech/ngspice/sky130.lib.spice \
	  --liberty $(GIVEN)/hd_reduced.lib --ngspice "$(TUT3)/tools.sh ngspice" --jobs $(INNER_JOBS) \
	  --workdir $(TUT3)/build/dd2_demo_sim_$(DEMO_S) --out $(DEMO_R)/transistor_level/$(DEMO_S).json
	$(LINUX) python3 src/model_level_check.py --request $(DEMO_C)/request.json \
	  --outdir $(DEMO_C) --report $(DEMO_R)/model_level/$(DEMO_S).json
	@echo "== 8 characterization, Liberty view, response; HD comparison; round trip"
	$(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(IMAGE) \
	  --jobs $(INNER_JOBS) --cell-dir $(DEMO_C) --out $(DEMO_C)/characterized
	$(PYTHON) src/liberty_view.py --cell-dir $(DEMO_C) --report $(DEMO_R)/views/$(DEMO_S).json
	$(LINUX) python3 src/model_level_check.py --request $(DEMO_C)/request.json \
	  --outdir $(DEMO_C) --report $(DEMO_R)/model_level/$(DEMO_S).json --liberty $(DEMO_C)/cell.lib
	@# Only a request with an installed HD counterpart (hd_reference.json) is compared:
	@# compare_hd.py fails when it has no cell to compare.
	@if $(PYTHON) -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get('reference') else 1)" \
	    $(DEMO_C)/hd_reference.json; then \
	  $(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME) \
	    --generated $(DEMO_DIR)/cells --outdir $(DEMO_R)/hd_comparison; \
	else echo "$(DEMO_CELL) has no installed HD counterpart: HD comparison not run"; fi
	$(PYTHON) src/write_response.py --cell-dir $(DEMO_C) --results $(DEMO_R)
	$(LINUX) python3 src/round_trip.py --cell-dir $(DEMO_C) --reduced $(GIVEN)/hd_reduced.lib \
	  --map-script $(GIVEN)/scripts/map.sh --outdir $(DEMO_R)/round_trip --workdir $(DEMO_DIR)/round_trip_work
	@echo "PASS: $(DEMO_CELL); response $(DEMO_C)/response.json"

clean:
	rm -rf build
