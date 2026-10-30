# AI mistakes log (overview, Appendix B)

| ID | Log entry | What was wrong | How you caught it | Would a weaker check have missed it? |
|---|---|---|---|---|
| M1 | U1 | The new `make check` piped the ngspice stress test through `tee`, so a stock ngspice (stress test exit 1) would still have passed `make check` | Re-reading the target: a pipeline's status is the last command's | Yes: on the fixed image the test passes either way, so only a stock build would show it |
| M2 | U1 | `transistor_level_check.py` passed the netlist and models as `DD2_Project_1/../student/...`; inside the container the repo is not mounted, so ngspice could not open them | `make sim` failed with "Could not find include file" | No: ngspice stops with a fatal error |
