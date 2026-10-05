# region-edit

Lets many agents edit the same file at once without lost updates or wasted tokens. Agents read and edit functions, methods and sections instead of whole files, and every edit is checked against the version it was based on. No server or background process: sessions coordinate through a short lock on the file itself.

## Install

Needs Python 3.9+, nothing else. In Claude Code:

```text
/plugin marketplace add MGross21/region-edit
/plugin install region-edit@region-edit
```

Other MCP clients: run `python3 server/region_edit.py` as a stdio server.

## Benchmark

8 agents making 32 edits to the same two functions of a 1.5k-line file, 30 runs. `python3 bench/bench.py` to reproduce.

| Strategy | Tokens | Correct |
| --- | ---: | ---: |
| Whole-file Read + Write | 555k | 0% |
| Read + Edit (Claude Code) | 1.34M | 97% |
| str_replace (aider, OpenHands) | 83k | 100% |
| Line-range edit (SWE-agent) | 85k | 0% |
| Unified diff / apply_patch | 343k | 100% |
| Worktree + 3-way merge | 129k | 100% |
| Lock per edit | 325k | 100% |
| **region-edit** | **6k** | **100%** |

Correct is the worst result across three workloads. With 8 real processes editing one file at full speed, plain read-modify-write lost ~88% of edits; region-edit lost none.

## License

MIT
