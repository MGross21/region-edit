<h1 align="center">Region Edit</h1>

<p align="center">
  <img src="https://img.shields.io/github/v/tag/MGross21/region-edit?label=version&color=5b8cff" alt="Version">
  <img src="https://img.shields.io/github/license/MGross21/region-edit?color=5b8cff" alt="License">
  <img src="https://img.shields.io/badge/python-3.9%2B-5b8cff" alt="Python 3.9+">
  <a href="https://github.com/MGross21/region-edit/stargazers"><img src="https://img.shields.io/github/stars/MGross21/region-edit?color=5b8cff" alt="Stars"></a>
</p>

The most token-efficient way for multiple AI agents to edit the same file. Zero lost edits.

Agents read and edit individual functions, methods and sections instead of whole files. Every edit is checked against the version it was based on, and sessions coordinate through a short file lock. No server, no dependencies.

## Install

Needs Python 3.9+. In Claude Code:

```text
/plugin marketplace add MGross21/region-edit
/plugin install region-edit@region-edit
```

Other MCP clients: run `python3 server/region_edit.py` as a stdio server. Only files under your home folder can be edited; set `REGION_EDIT_ROOT` to change that.

## Benchmark

8 agents, 32 edits to the same two functions of a 1.5k-line file, 30 runs.

| Strategy | Tokens | Correct (worst case) |
| --- | ---: | ---: |
| ⭐ **Region Edit** | **6k** | **100%** |
| str_replace (aider, OpenHands) | 83k | 100% |
| Line-range edit (SWE-agent) | 85k | 0% |
| Worktree + 3-way merge | 129k | 100% |
| Lock whole cycle | 325k | 100% |
| Unified diff / apply_patch | 343k | 100% |
| Whole-file Read + Write | 555k | 0% |
| Read + Edit (Claude Code) | 1.34M | 97% |

Under real concurrent writes, plain read-modify-write lost 88% of edits. Region Edit lost none. Reproduce with `python3 bench/bench.py`.

## License

MIT
