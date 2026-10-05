#!/usr/bin/env python3
"""
region-edit: multi-agent shared-file editing over MCP (stdio).

Tools: outline, read, edit, replace_region, insert.

Usage: python3 region_edit.py [--root DIR]
  --root DIR   only files under DIR can be edited (default: $REGION_EDIT_ROOT or home)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

if os.name == "nt":
    import msvcrt
    import time
else:
    import fcntl

VERSION = "0.1.0"
SNAP_MAX = 4000
HASH_CHARS = 8
LOCK_NAME_CHARS = 24
TEMP_SUFFIX_BYTES = 4
DIFF_CONTEXT_LINES = 1
LOCK_RETRY_SECONDS = 0.001
CHUNK_LINES = 40
JSONRPC_METHOD_NOT_FOUND = -32601
# shared by every session on this machine
LOCK_DIR = os.path.join(os.path.expanduser("~"), ".cache", "region-edit", "locks")


def h8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:HASH_CHARS]


class Region:
    __slots__ = ("id", "start", "end")  # line indexes, end exclusive

    def __init__(self, id: str, start: int, end: int):
        self.id, self.start, self.end = id, start, end


class Lang:
    __slots__ = ("top", "name", "prefix", "member", "closer")

    def __init__(self, top, name, prefix, member=None, closer=None):
        self.top = top
        self.name = name  # match -> (id, is_container)
        self.prefix = prefix
        self.member = member  # groups: (indent, name)
        self.closer = closer


_RS_MOD = r"(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+\"[^\"]*\")?)\s+)*"
_JS_MOD = r"(?:(?:public|private|protected|static|readonly|async|override|abstract|get|set|declare)\s+)*"
_NOT_MEMBERS = {"if", "for", "while", "switch", "catch", "return", "function", "with", "elif", "else"}


def _rs_name(m: re.Match) -> tuple[str, bool]:
    kind, rest = m[1], (m[2] or "").strip()
    if kind != "impl":
        rest = re.match(r"\w*", rest)[0]
    return (f"{kind} {rest}".strip(), kind in ("impl", "trait"))


def _go_name(m: re.Match) -> tuple[str, bool]:
    if m[3]:
        return m[3], False
    return (f"{m[1]}.{m[2]}" if m[1] else m[2]), False


_PY = Lang(
    top=re.compile(r"^(?:async\s+def|def|class)\s+(\w+)"),
    name=lambda m: (m[1], m[0].startswith("class")),
    prefix=re.compile(r"^\s*@"),
    member=re.compile(r"^([ \t]+)(?:async\s+def|def|class)\s+(\w+)"),
)
_RS = Lang(
    top=re.compile(r"^" + _RS_MOD + r"(fn|struct|enum|union|trait|impl|mod|type|const|static|macro_rules!)"
                   r"\s*(?:<[^>]*>\s*)?([\w:<>, ]+)?"),
    name=_rs_name,
    prefix=re.compile(r"^\s*(?:#\[|///|//!)"),
    member=re.compile(r"^([ \t]+)" + _RS_MOD + r"fn\s+(\w+)"),
    closer=re.compile(r"^\}"),
)
_JS = Lang(
    top=re.compile(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
                   r"(?:(function\*?|class|interface|type|enum|namespace)\s+([\w$]+)"
                   r"|(?:const|let|var)\s+([\w$]+)\s*(?::[^=]+)?=(?=.*(?:=>|\bfunction\b)))"),
    name=lambda m: (m[2] or m[3], m[1] == "class"),
    prefix=re.compile(r"^\s*(?:@|/\*\*|\*|//)"),
    member=re.compile(r"^([ \t]+)" + _JS_MOD + r"\*?(#?[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*"
                      r"(?:\(|=\s*(?:async\s+)?(?:\([^)]*\)|[\w$]+)\s*(?::[^=]+)?=>)"),
    closer=re.compile(r"^\}"),
)
_GO = Lang(
    top=re.compile(r"^(?:func\s+(?:\(\s*(?:\w+\s+)?\*?(\w+)[^)]*\)\s*)?(\w+)|type\s+(\w+))"),
    name=_go_name,
    prefix=re.compile(r"^//"),
)
LANGS = {".py": _PY, ".pyi": _PY, ".rs": _RS, ".go": _GO,
         **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"), _JS)}
MD_HEAD = re.compile(r"^(#{1,6})\s+(.*\S)")


def _attach(prefix, lines: list[str], i: int, floor: int) -> int:
    while i > floor and prefix.match(lines[i - 1]):
        i -= 1
    return i


def split_regions(suffix: str, lines: list[str]) -> list[Region]:
    starts: list[tuple[int, str]] = []  # id "" = generic chunk
    lang = LANGS.get(suffix)
    if lang:
        tops, floor = [], 0
        for i, ln in enumerate(lines):
            m = lang.top.match(ln)
            if m:
                rid, box = lang.name(m)
                tops.append((_attach(lang.prefix, lines, i, floor), i, rid, box))
                floor = i + 1
        for k, (s, d, cid, box) in enumerate(tops):
            starts.append((s, cid))
            if not (box and lang.member):
                continue
            e = end = tops[k + 1][0] if k + 1 < len(tops) else len(lines)
            if lang.closer:
                end = next((i for i in range(e - 1, d, -1) if lang.closer.match(lines[i])), None)
                if end is None:
                    continue
            indent, floor, members = None, d + 1, []
            for i in range(d + 1, end):
                m = lang.member.match(lines[i])
                if not m or m[2] in _NOT_MEMBERS:
                    continue
                indent = indent or m[1]
                if m[1] == indent:
                    members.append((_attach(lang.prefix, lines, i, floor), f"{cid}.{m[2]}"))
                    floor = i + 1
            starts += members
            if members and lang.closer:
                starts.append((end, f"{cid}.end"))
    elif suffix in (".md", ".markdown", ".mdx"):
        fenced = False
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            elif not fenced:
                m = MD_HEAD.match(ln)
                if m:
                    starts.append((i, f"{m[1]} {m[2]}"))
    else:
        cur = 0
        for i, ln in enumerate(lines):
            if i - cur >= CHUNK_LINES and ln.strip() == "" and i + 1 < len(lines):
                starts.append((i + 1, ""))
                cur = i + 1

    first = starts[0][0] if starts else len(lines)
    regs = [Region("<head>", 0, first)] if first > 0 or not starts else []
    seen: dict[str, int] = {}
    for k, (s, name) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        if e <= s:
            continue
        if not name:
            name = "chunk@" + h8(next((l for l in lines[s:e] if l.strip()), ""))
        n = seen[name] = seen.get(name, 0) + 1
        regs.append(Region(name if n == 1 else f"{name}#{n}", s, e))
    return regs


class Doc:
    """Caller must hold the file's lock."""

    def __init__(self, path: str, snaps: dict[str, str]):
        self.path, self.snaps, self.dirty = path, snaps, False
        try:
            with open(path, encoding="utf-8", newline="") as f:
                self.text = f.read()
        except FileNotFoundError:
            self.text = ""
        self._split()

    def _split(self):
        self.lines = self.text.splitlines(keepends=True)
        self.regs = split_regions(os.path.splitext(self.path)[1].lower(), self.lines)

    def _snap(self, text: str) -> str:
        h = h8(text)
        self.snaps.pop(h, None)
        self.snaps[h] = text
        if len(self.snaps) > SNAP_MAX:
            del self.snaps[next(iter(self.snaps))]
        return h

    def _body(self, r: Region) -> str:
        return "".join(self.lines[r.start:r.end])

    def _find(self, rid: str):
        r = next((x for x in self.regs if x.id == rid), None)
        return r, (self._body(r) if r else None)

    def _commit(self, r: Region, new_region: str):
        self.text = "".join(self.lines[:r.start]) + new_region + "".join(self.lines[r.end:])
        self.dirty = True
        self._split()

    def _done(self, rid: str, before: set[str], rebased=False) -> str:
        r = self._find(rid)[0] if rid else None
        rows = ["ok" + (f" h={self._snap(self._body(r))}" if r else "") + (" rebased" if rebased else "")]
        rows += [f"+{x.id}\t{self._snap(self._body(x))}" for x in self.regs if x.id not in before]
        if rid and r is None:
            rows.append("region id changed or removed; call outline()")
        return "\n".join(rows)

    def _conflict(self, h: str, cur: str) -> str:
        base, head = self.snaps.get(h), f"conflict h={self._snap(cur)}\n"
        if base is None:
            return head + cur
        import difflib
        return head + "".join(difflib.unified_diff(base.splitlines(True), cur.splitlines(True), "yours", "now", n=DIFF_CONTEXT_LINES))

    def outline(self) -> str:
        return "\n".join(f"{r.id}\t{r.start + 1}-{r.end}\t{self._snap(self._body(r))}" for r in self.regs)

    def read(self, rid: str) -> str:
        r, cur = self._find(rid)
        if r is None:
            return f"error: no_such_region {rid}; call outline()\n"
        return f"{rid}\t{r.start + 1}-{r.end}\t{self._snap(cur)}\n{cur}" + ("" if cur.endswith("\n") else "\n")

    def edit(self, rid: str, h: str, old: str, new: str) -> str:
        if not old:
            return "error: empty_anchor"
        r, cur = self._find(rid)
        if r is None:
            return f"error: no_such_region {rid}; call outline()"
        n = cur.count(old)
        moved = h8(cur) != h
        if n != 1:
            if moved:
                return self._conflict(h, cur)
            return "error: anchor_not_found" if n == 0 else f"error: anchor_matches_{n}x"
        before = {x.id for x in self.regs}
        self._commit(r, cur.replace(old, new, 1))
        return self._done(rid, before, rebased=moved)

    def replace_region(self, rid: str, h: str, new: str) -> str:
        r, cur = self._find(rid)
        if r is None:
            return f"error: no_such_region {rid}; call outline()"
        if h8(cur) != h:
            return self._conflict(h, cur)
        before = {x.id for x in self.regs}
        self._commit(r, new + "\n" if new and not new.endswith("\n") else new)
        return self._done(rid, before)

    def insert(self, after: str, text: str) -> str:
        before = {x.id for x in self.regs}
        if after == "<end>":
            if self.lines and not self.lines[-1].endswith("\n"):
                self.lines[-1] += "\n"
            pos = len(self.lines)
        else:
            r = self._find(after)[0]
            if r is None:
                return f"error: no_such_region {after}; call outline()"
            pos = r.end
        self._commit(Region(after, pos, pos), text if text.endswith("\n") else text + "\n")
        return self._done("", before)


class Store:
    OPS = ("outline", "read", "edit", "replace_region", "insert")

    def __init__(self, root: str):
        self.root = os.path.realpath(os.path.expanduser(root))
        self.snaps: dict[str, str] = {}
        os.makedirs(LOCK_DIR, exist_ok=True)

    def call(self, path: str, op: str, *args) -> str:
        if op not in self.OPS:
            raise ValueError(f"unknown op {op}")
        p = os.path.realpath(os.path.expanduser(path))
        if os.path.commonpath([self.root, p]) != self.root:
            raise ValueError(f"path outside root {self.root}")
        lock_path = os.path.join(LOCK_DIR, hashlib.sha256(p.encode()).hexdigest()[:LOCK_NAME_CHARS])
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(LOCK_RETRY_SECONDS)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)  # released by os.close
            d = Doc(p, self.snaps)
            if op == "read":
                out = "".join(d.read(r) for r in (args[0] if isinstance(args[0], list) else [args[0]]))
            else:
                out = getattr(d, op)(*args)
            if d.dirty:
                folder = os.path.dirname(p)
                os.makedirs(folder, exist_ok=True)
                tmp = os.path.join(folder, f".{os.path.basename(p)}.{os.urandom(TEMP_SUFFIX_BYTES).hex()}")
                tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
                try:
                    with os.fdopen(tfd, "w", encoding="utf-8", newline="") as f:
                        f.write(d.text)
                        f.flush()
                        os.fsync(f.fileno())
                    try:
                        os.chmod(tmp, os.stat(p).st_mode)
                    except FileNotFoundError:
                        pass
                    os.replace(tmp, p)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            return out
        finally:
            if os.name == "nt":
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(fd)


INSTRUCTIONS = """Shared-file editor for multiple agents. Never read or write whole files.
Paths may be absolute or relative to your working directory.
Workflow: outline(path) -> read(path, region) only for regions you need (a list reads several) ->
edit(path, region, h, old, new) with the SMALLEST unique `old` anchor (1-2 lines).
Keep the returned h for your next edit of that region.
rebased: someone else also changed this region; your edit applied, re-read if it matters.
conflict: apply your intent to the returned diff/text using the new h. Do not re-read the file.
Use replace_region to rewrite or delete (new="") a region without resending its old text.
Use insert(path, after, text) for new code; after="<end>" appends (also creates new files)."""

_S = {"type": "string"}
TOOLS = {  # name -> (description, params in call order)
    "outline": ("Region map: one line per region `id<TAB>lines<TAB>hash`.", {"path": _S}),
    "read": ("Text of one region, or a list of regions, each headed `id<TAB>lines<TAB>hash`.",
             {"path": _S, "region": {"anyOf": [_S, {"type": "array", "items": _S}]}}),
    "edit": ("Replace a unique anchor `old` with `new` inside one region. `h` = region hash you based this on.",
             {"path": _S, "region": _S, "h": _S, "old": _S, "new": _S}),
    "replace_region": ('Rewrite a whole region (new="" deletes it). Strict: fails with a diff if the region changed.',
                       {"path": _S, "region": _S, "h": _S, "new": _S}),
    "insert": ('Insert text after region `after` ("<end>" appends). Returns ids+hashes of new regions.',
               {"path": _S, "after": _S, "text": _S}),
}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def handle(store: Store, method: str, params: dict):
    if method == "initialize":
        v = params.get("protocolVersion")
        return {"protocolVersion": v if v in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "region-edit", "version": VERSION},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p,
                                                                         "required": list(p)}}
                          for n, (d, p) in TOOLS.items()]}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name not in TOOLS:
                raise ValueError(f"unknown tool {name}")
            text = store.call(args["path"], name, *(args[k] for k in list(TOOLS[name][1])[1:]))
            err = False
        except KeyError as e:
            text, err = f"error: missing argument {e}", True
        except Exception as e:
            text, err = f"error: {type(e).__name__}: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": err}
    raise LookupError(method)


def main():
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return
    root = argv[argv.index("--root") + 1] if "--root" in argv else os.environ.get("REGION_EDIT_ROOT", "~")
    store = Store(root)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or "id" not in msg or "method" not in msg:
            continue
        try:
            resp = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(store, msg["method"], msg.get("params") or {})}
        except LookupError:
            resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": JSONRPC_METHOD_NOT_FOUND, "message": "method not found"}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
