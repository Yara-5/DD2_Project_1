# DD2 Project 1, Option 1: the targets the overview requires (section 5.1).
#
#   make setup     link the PDK into Tutorial 3's folder (its tools.sh needs it)
#   make check     package pins, tools, PDK, given files, ngspice stress test, rules vs PDK
#   make all       the whole flow for every cell in CELLS
#   make test      make all, then every broken case in tests/broken/ must be rejected
#   make demo IN=tests/requests/<cell>.json
#   make char_hd   characterize the six installed reference cells (section 5.1)
#   make char      characterize the generated cells in CELLS (section 5)
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
-include local.mk

PYTHON   ?= python3
LINUX    ?=
PDK_ROOT ?= $(HOME)/.ciel
PDK      ?= $(PDK_ROOT)/sky130A
PDK_HOST ?= $(PDK)
GIVEN    ?= ../DD2_Project1_student_pack/given
TUT3     ?= ../student
IMAGE    ?= dd2-layout-tools
NETWORK_CELLS ?= $(sort $(basename $(notdir $(wildcard tests/requests/*.json))))
CELLS    ?= $(NETWORK_CELLS)
OBJECTIVE ?= inverters
PREFIX    = dd2_g5__

LPYTHON   = $(LINUX) env LAYOUT_IMAGE=$(IMAGE) python3
FRAME     = $(GIVEN)/plan_hd.json
HD_GDS    = $(PDK_HOST)/libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds
HD_SPICE  = $(PDK)/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice
HD_SPICE_HOST = $(PDK_HOST)/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice
TECH      = $(PDK)/libs.tech/magic/sky130A.tech
HD_LIB    = $(PDK)/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
HD_GDS_LINUX = $(PDK)/libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds
REFERENCE_CELLS = inv_1 nand2_1 nor2_1 nand3_1 a21oi_1 o21ai_1

# A broken case passes only when its check exits non-zero.
define expect_fail
	@mkdir -p build/broken
	@if $(2) > build/broken/$(1).log 2>&1; then echo "FAIL: broken case $(1) was accepted (build/broken/$(1).log)"; exit 1; \
	 else echo "ok   broken case $(1) rejected"; fi
endef

.PHONY: setup check test all demo clean network layout drc lvs compare sim model char_hd char

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

network:          ## steps 1-3: read, minimize, decompose into stages, size; then the switch-level check
	@for c in $(NETWORK_CELLS); do \
	  $(PYTHON) src/request_reader.py tests/requests/$$c.json --out cells/$(PREFIX)$$c/request.json > /dev/null && \
	  $(PYTHON) src/synthesize.py --request cells/$(PREFIX)$$c/request.json --frame $(FRAME) \
	    --outdir cells/$(PREFIX)$$c --objective $(OBJECTIVE) | head -1 && \
	  $(PYTHON) src/switch_level_check.py cells/$(PREFIX)$$c/network.json --request cells/$(PREFIX)$$c/request.json \
	    --out results/switch_level/$${c}_network.json || exit 1; done

layout: network   ## steps 4-5: Euler paths, then plan and draw; the reordered network is checked again
	@for c in $(CELLS); do \
	  $(PYTHON) src/euler.py cells/$(PREFIX)$$c/network.json --hd-spice $(HD_SPICE_HOST) && \
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
	$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --image $(IMAGE)

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

char_hd:          ## section 5.1: characterize the six installed reference cells
	@for c in $(REFERENCE_CELLS); do \
	  $(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(IMAGE) \
	    --hd-cell sky130_fd_sc_hd__$$c --hd-gds $(HD_GDS_LINUX) --pdk-spice $(HD_SPICE) \
	    --out results/characterization/hd/$$c || exit 1; done

char:             ## section 5: characterize the generated cells (only after char_hd is validated)
	@for c in $(CELLS); do \
	  $(LPYTHON) src/characterize.py --tutorial-root $(TUT3) --liberty $(HD_LIB) --image $(IMAGE) \
	    --cell-dir cells/$(PREFIX)$$c --out cells/$(PREFIX)$$c/characterized || exit 1; done

all: network layout drc lvs compare sim model

test: all         ## every broken case must be rejected by its check
	$(call expect_fail,bad_request,$(PYTHON) src/request_reader.py tests/broken/bad_request.json)
	$(call expect_fail,rules_wrong_poly_space,$(LPYTHON) src/pdk_rules.py --rules tests/broken/rules_wrong_poly_space.json --tech $(TECH) --frame $(FRAME) --out build/broken/pdk_rules.json)
	$(call expect_fail,nand2_1_missing_pullup,$(PYTHON) src/switch_level_check.py tests/broken/nand2_1_missing_pullup.json)
	$(call expect_fail,and2_1_network_missing_pullup,$(PYTHON) src/switch_level_check.py tests/broken/and2_1_network_missing_pullup.json)
	@rm -rf build/broken/lvs && mkdir -p build/broken/lvs/$(PREFIX)nand2_1 && \
	  cp cells/$(PREFIX)nand2_1/cell.gds cells/$(PREFIX)nand2_1/input.json build/broken/lvs/$(PREFIX)nand2_1/ && \
	  cp tests/broken/nand2_1_wrong_width.spice build/broken/lvs/$(PREFIX)nand2_1/schematic.spice
	$(call expect_fail,nand2_1_wrong_width,$(LPYTHON) src/verify_lvs.py --tutorial-root $(TUT3) --pdk-spice $(HD_SPICE) --image $(IMAGE) --cells build/broken/lvs --outdir build/broken/lvs_results)
	$(call expect_fail,inv_1_no_hvtp,$(PYTHON) src/compare_hd.py --reference-gds $(HD_GDS) --frame $(FRAME) --generated tests/broken/frame --outdir build/broken/frame_results)
	$(call expect_fail,nand2_1_pex_missing_pullup,$(LPYTHON) src/transistor_level_check.py --pex tests/broken/nand2_1_pex_missing_pullup.spice --request cells/$(PREFIX)nand2_1/request.json --models $(TUT3)/pdk/libs.tech/ngspice/sky130.lib.spice --liberty $(GIVEN)/hd_reduced.lib --ngspice "$(TUT3)/tools.sh ngspice" --workdir $(TUT3)/build/dd2_sim_broken --out build/broken/transistor_level.json)
	@echo "PASS: every broken case was rejected (DRC and layer-check broken cases run inside make drc)"

demo:             ## what the interview runs: make demo IN=tests/requests/<cell>.json
	@test -n "$(IN)" || (echo "usage: make demo IN=tests/requests/<cell>.json" && exit 1)
	$(MAKE) all NETWORK_CELLS=$(basename $(notdir $(IN))) CELLS=$(basename $(notdir $(IN)))

clean:
	rm -rf build
