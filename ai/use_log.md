# AI use log (overview, Appendix B)

| ID | Date | Who | Tool and model | What you asked | What you kept or changed | How you checked it | Commit |
|---|---|---|---|---|---|---|---|
| U1 | 2026-10-30 | Youssef | Claude Code, Claude Opus 5.5 | Write AGENTS.md (used as CLAUDE.md), review src/ against it, run the whole flow and fix bugs | Restructured the repo (code in src/, inputs in tests/, outputs in cells/ and results/); added rules.json + pdk_rules.py, verify_lvs.py, netgen.sh, the Makefile targets and broken cases; generalized the switch, transistor and model checks to the request's truth table (check scripts changed on the user's explicit request, AGENTS.md rule 9) | `make check`, `make test` (whole flow plus every broken case); regenerated GDS byte-identical to the previous commit | (uncommitted) |
