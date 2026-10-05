---
name: region-edit
description: Use when more than one agent or subagent may edit the same file — parallel subagents, teammates working in one repo, or any task that says files are shared. Routes those edits through the region-edit tools so no edit is lost and token use stays low.
---

# Editing files other agents also edit

For any file another agent might touch during this task, use the region-edit
MCP tools instead of Read, Edit and Write. The built-in tools read and rewrite
whole files, which wastes tokens and can silently overwrite another agent's work.

## Workflow

1. `outline(path)` returns one line per region: `id`, line range, 8-char hash.
   Regions are functions, classes and their methods (`Cls.method`), Rust
   impl/trait members (`impl Foo.bar`), Go funcs, JS/TS functions and class
   members, Markdown headings, or ~40-line chunks (anything else).
2. `read(path, region)` only for regions you need; pass a list to read several
   in one call. Keep each region's hash.
3. `edit(path, region, h, old, new)` with the smallest unique `old` anchor,
   usually 1–2 lines. Keep the new `h` it returns for your next edit.
4. New code: `insert(path, after, text)`; `after="<end>"` appends.
   `after="impl Foo.end"` (or `Cls.end` in JS/TS) adds after the closing brace.
   Rewrite or delete a region: `replace_region(path, region, h, new)` (`new=""` deletes).

## Results

- `ok h=…` — applied; `h` is the region's new hash.
- `rebased` — someone else changed that region too, but your anchor was
  intact so your edit applied. Re-read the region only if their change could
  affect yours.
- `conflict h=…` — the region changed under you. Apply your intent to the
  returned diff (or text) with the returned `h`. Do not re-read the whole file.
- `error: anchor_not_found` / `anchor_matches_Nx` — fix your anchor.
- `error: no_such_region` — ids changed; call `outline` again.

## When starting subagents

Tell each subagent which files are shared and that it must edit them with the
region-edit tools. Where possible, give each subagent its own regions to own.

After all agents finish, run the project's checks (build, tests, linters):
edits that don't conflict textually can still conflict in meaning.
