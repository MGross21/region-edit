#!/usr/bin/env python3
"""
Benchmark region-edit against common agent file-editing strategies.

Part 1: simulated agents interleaving think time and atomic tool calls on one file;
tokens counted from tool results (read) and tool arguments (written).
Part 2: real processes editing one file at full speed; region-edit runs as one
MCP server process per session.

    python3 bench/bench.py
    python3 bench/bench.py --seeds 50
    python3 bench/bench.py --baseline old/region_edit.py
"""
from __future__ import annotations

import argparse
import heapq
import importlib.util
import itertools
import json
import math
import multiprocessing as mp
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server" / "region_edit.py"
CHARS_PER_TOKEN = 4
MAX_TRIES = 30
THINK_SECONDS = (0.5, 1.5)
REREAD_THINK_SCALE = 0.3
START_JITTER_SECONDS = 0.2
READ_NUMBER_WIDTH = 6
EDIT_ECHO_LINES = 4
PATCH_CONTEXT_LINES = 3
SWE_WINDOW_BEFORE, SWE_WINDOW_AFTER = 5, 15
FASTMCP_JSON_INDENT = 2
N_CLASSES, METHODS_PER_CLASS, N_FUNCS, BODY_LINES = 3, 8, 48, 18
EDITS_DISJOINT, EDITS_HOT, EDITS_GROWING, INSERTS_GROWING, INSERT_BODY_LINES = 6, 4, 4, 2, 6
P99 = 0.99
MS_PER_S = 1000


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def tok(s: str) -> int:
    return math.ceil(len(s) / CHARS_PER_TOKEN)


def numbered(text: str, start: int = 1) -> str:
    return "".join(f"{i:>{READ_NUMBER_WIDTH}}\t{l}" for i, l in enumerate(text.splitlines(True), start))


@dataclass
class Unit:
    name: str
    top: bool
    lines: list[str]


@dataclass
class Task:
    unit: Unit
    old: str
    new: str
    anchor: str
    anchor_new: str
    insert: str = ""  # non-empty for inserts


def edit_task(u: Unit, j: int, agent: int) -> Task:
    line = u.lines[j]
    new = f"{line} + 1  # a{agent}"
    return Task(u, line + "\n", new + "\n", line.strip(), new.strip())


@dataclass
class Agent:
    tin: int = 0
    tout: int = 0
    calls: int = 0
    retries: int = 0

    def call(self, args: str, result: str):
        self.calls += 1
        self.tout += tok(args)
        self.tin += tok(result)


class FS:
    def __init__(self, text: str):
        self.text, self.ver = text, 0

    def write(self, text: str):
        self.text, self.ver = text, self.ver + 1


def think(rng, scale=1.0):
    return ("think", rng.uniform(*THINK_SECONDS) * scale)


def snippet(text: str, needle: str) -> str:
    lines = text.splitlines(True)
    i = next((k for k, l in enumerate(lines) if needle.splitlines()[0] in l), 0)
    a = max(0, i - EDIT_ECHO_LINES)
    return numbered("".join(lines[a:i + EDIT_ECHO_LINES + 1]), a + 1)




def s_whole_file(fs, ag, tasks, rng, ctx):
    for t in tasks:
        view = fs.text
        ag.call("Read path", numbered(view))
        yield think(rng)
        new = view.replace(t.old, t.new, 1) if view.count(t.old) == 1 else view
        fs.write(new)
        ag.call(new, "ok")


def s_cc_edit(fs, ag, tasks, rng, ctx):
    seen = fs.ver
    ag.call("Read path", numbered(fs.text))
    for t in tasks:
        yield think(rng)
        for _ in range(MAX_TRIES):
            if fs.ver != seen:
                ag.call(t.old + t.new, "File has been modified since read. Read it again before attempting to write it.")
                ag.retries += 1
                seen = fs.ver
                ag.call("Read path", numbered(fs.text))
                yield think(rng, REREAD_THINK_SCALE)
                continue
            if fs.text.count(t.old) != 1:
                ag.call(t.old + t.new, "String to replace not found in file.")
                ag.retries += 1
                seen = fs.ver
                ag.call("Read path", numbered(fs.text))
                yield think(rng, REREAD_THINK_SCALE)
                continue
            fs.write(fs.text.replace(t.old, t.new, 1))
            seen = fs.ver
            ag.call(t.old + t.new, snippet(fs.text, t.new))
            break


def s_str_replace(fs, ag, tasks, rng, ctx):
    ag.call("view path", numbered(fs.text))
    for t in tasks:
        yield think(rng)
        for _ in range(MAX_TRIES):
            if fs.text.count(t.old) == 1:
                fs.write(fs.text.replace(t.old, t.new, 1))
                ag.call(t.old + t.new, snippet(fs.text, t.new))
                break
            ag.call(t.old + t.new, "No replacement was performed: old_str not found.")
            ag.retries += 1
            ag.call("view path", numbered(fs.text))
            yield think(rng, REREAD_THINK_SCALE)


def s_line_range(fs, ag, tasks, rng, ctx):
    view = fs.text
    ag.call("open path", numbered(view))
    for t in tasks:
        yield think(rng)
        vl = view.splitlines(True)
        olines = t.old.splitlines(True)
        a = next(i for i in range(len(vl)) if vl[i:i + len(olines)] == olines)
        b = a + len(olines)
        cur = fs.text.splitlines(True)
        cur[a:b] = t.new.splitlines(True)
        fs.write("".join(cur))
        vl[a:b] = t.new.splitlines(True)
        view = "".join(vl)
        ag.call(f"edit {a + 1}:{b}\n{t.new}", numbered("".join(cur[max(0, a - SWE_WINDOW_BEFORE):a + SWE_WINDOW_AFTER]),
                                                         max(0, a - SWE_WINDOW_BEFORE) + 1))


def s_udiff(fs, ag, tasks, rng, ctx):
    view = fs.text
    ag.call("read path", numbered(view))
    for t in tasks:
        yield think(rng)
        for _ in range(MAX_TRIES):
            vl = view.splitlines(True)
            ol = t.old.splitlines(True)
            a = next(i for i in range(len(vl)) if vl[i:i + len(ol)] == ol)
            end = a + len(ol)
            pre, post = "".join(vl[max(0, a - PATCH_CONTEXT_LINES):a]), "".join(vl[end:end + PATCH_CONTEXT_LINES])
            hunk_old, hunk_new = pre + t.old + post, pre + t.new + post
            patch = "@@\n" + "".join(" " + l for l in pre.splitlines(True)) + "".join("-" + l for l in ol) \
                + "".join("+" + l for l in t.new.splitlines(True)) + "".join(" " + l for l in post.splitlines(True))
            if fs.text.count(hunk_old) == 1:
                fs.write(fs.text.replace(hunk_old, hunk_new, 1))
                view = view.replace(hunk_old, hunk_new, 1)
                ag.call(patch, "Done!")
                break
            ag.call(patch, "error: patch failed: context does not match")
            ag.retries += 1
            view = fs.text
            ag.call("read path", numbered(view))
            yield think(rng, REREAD_THINK_SCALE)


def s_worktree(fs, ag, tasks, rng, ctx):
    base = mine = fs.text
    ag.call("Read path", numbered(mine))
    for t in tasks:
        yield think(rng)
        mine = mine.replace(t.old, t.new, 1)
        ag.call(t.old + t.new, snippet(mine, t.new))
    with tempfile.TemporaryDirectory() as d:
        p = {k: Path(d, k) for k in ("ours", "base", "theirs")}
        p["ours"].write_text(fs.text), p["base"].write_text(base), p["theirs"].write_text(mine)
        r = subprocess.run(["git", "merge-file", "-p", str(p["ours"]), str(p["base"]), str(p["theirs"])],
                           capture_output=True, text=True)
    ag.calls += 1
    if r.returncode == 0:
        fs.write(r.stdout)
        return
    ctx["merge_conflicts"] = ctx.get("merge_conflicts", 0) + r.returncode
    ag.retries += 1
    ag.call("Read path", numbered(r.stdout))
    yield think(rng)
    text = fs.text
    for t in tasks:
        text = text.replace(t.old, t.new, 1)
        ag.call(t.old + t.new, snippet(text, t.new))
    fs.write(text)


def s_lock(fs, ag, tasks, rng, ctx):
    for t in tasks:
        yield ("lock",)
        ag.call("Read path", numbered(fs.text))
        yield think(rng)
        fs.write(fs.text.replace(t.old, t.new, 1))
        ag.call(t.old + t.new, snippet(fs.text, t.new))
        yield ("unlock",)


def s_region_edit(fs, ag, tasks, rng, ctx):
    call = ctx["session"]()

    def outline():
        ag.call("outline path", call("outline")[0])

    outline()
    hs: dict[str, str] = {}
    for t in tasks:
        rid = ctx["region_for"](t.unit)
        if rid not in hs:
            text, info = call("read", rid)
            ag.call(f"read path {rid}", text)
            hs[rid] = info["h"]
        yield think(rng)
        for _ in range(MAX_TRIES):
            if t.insert:
                text, info = call("insert", rid, t.insert)
                ag.call(f"insert path {rid} {t.insert}", text)
            else:
                text, info = call("edit", rid, hs[rid], t.anchor, t.anchor_new)
                ag.call(f"edit path {rid} {hs[rid]} {t.anchor} {t.anchor_new}", text)
            if info.get("ok"):
                hs[rid] = info.get("h", hs[rid])
                break
            ag.retries += 1
            if info.get("conflict"):
                hs[rid] = info["h"]
                continue
            outline()


def _rmw_worker(path, k, n, atomic, barrier):
    pat = re.compile(rf"line_{k} = (\d+)\n")
    barrier.wait()
    for _ in range(n):
        text = Path(path).read_text()
        m = pat.search(text)
        if not m:
            continue
        text = text[:m.start()] + f"line_{k} = {int(m[1]) + 1}\n" + text[m.end():]
        if atomic:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, path)
        else:
            Path(path).write_text(text)


def _mcp_worker(path, root, rid, k, n, barrier, out):
    pr = subprocess.Popen([sys.executable, str(SERVER), "--root", root],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    seq = 0

    def rpc(method, params):
        nonlocal seq
        seq += 1
        pr.stdin.write(json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params}) + "\n")
        pr.stdin.flush()
        return json.loads(pr.stdout.readline())["result"]

    def tool(name, **args):
        return rpc("tools/call", {"name": name, "arguments": {"path": path, **args}})["content"][0]["text"]

    rpc("initialize", {"protocolVersion": "2025-06-18"})
    h = tool("read", region=rid).split("\n")[0].split("\t")[2]
    v, lat, rebased = 0, [], 0
    barrier.wait()
    for _ in range(n):
        t0 = time.perf_counter()
        r = tool("edit", region=rid, h=h, old=f"line_{k} = {v}\n", new=f"line_{k} = {v + 1}\n")
        lat.append(time.perf_counter() - t0)
        head = r.split("\n")[0]
        if not head.startswith("ok"):
            raise RuntimeError(r)
        rebased += "rebased" in head
        h, v = head.split("h=")[1].split()[0], v + 1
    pr.stdin.close()
    pr.wait()
    out.append((lat, rebased))


def _count(text):
    return sum(int(l.split("= ")[1]) for l in text.splitlines() if "line_" in l and "= " in l)


def tool_schema_tokens(mod):
    if hasattr(mod, "handle"):
        tools = mod.handle(None, "tools/list", {})["tools"]
    else:
        import asyncio

        class Dummy:
            def call(self, *a):
                return ""

        try:
            tools = [t.model_dump(exclude_none=True) for t in asyncio.run(mod.build_server(Dummy()).list_tools())]
        except ImportError:
            return None
    return tok(json.dumps(tools) + mod.INSTRUCTIONS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--agents", type=int, default=8)
    ap.add_argument("--edits", type=int, default=200, help="edits per process in part 2")
    ap.add_argument("--baseline", type=Path, help="older region_edit.py to compare")
    ap.add_argument("--skip-sim", action="store_true")
    ap.add_argument("--skip-real", action="store_true")
    a = ap.parse_args()
    n_agents = a.agents

    cur = load(SERVER, "region_edit")
    mods = {f"region-edit {cur.VERSION}": (cur, lambda u: u.name)}
    if a.baseline:
        old = load(a.baseline, "region_edit_baseline")
        mods[f"region-edit {old.VERSION}"] = (old, lambda u: f"def {u.name}" if u.top else f"class {u.name.split('.')[0]}")
    strategies = {
        "whole-file Read+Write": s_whole_file,
        "Read + Edit (Claude Code)": s_cc_edit,
        "str_replace, no check (aider, OpenHands)": s_str_replace,
        "line-range edit (SWE-agent)": s_line_range,
        "unified diff / apply_patch": s_udiff,
        "worktree + 3-way merge": s_worktree,
        "lock whole cycle": s_lock,
        **dict.fromkeys(mods, s_region_edit),
    }

    if not a.skip_sim:
        out, units, serial = ['"""Generated fixture."""', "import math", ""], [], itertools.count()
        for c in range(N_CLASSES):
            out += [f"class C{c}:", f'    """Class {c}."""', ""]
            for m in range(METHODS_PER_CLASS):
                lines = [f"        v_c{c}m{m}_{j} = {next(serial)}" for j in range(BODY_LINES)]
                out += [f"    def m{m}(self, x):", *lines, "        return x", ""]
                units.append(Unit(f"C{c}.m{m}", False, lines))
        for f in range(N_FUNCS):
            lines = [f"    v_f{f}_{j} = {next(serial)}" for j in range(BODY_LINES)]
            out += [f"def f{f}(x):", *lines, "    return x", "", ""]
            units.append(Unit(f"f{f}", True, lines))
        base = "\n".join(out) + "\n"
        tops = [u for u in units if u.top]

        print(f"\nPart 1: {n_agents} agents, one {len(base.splitlines())}-line file (~{tok(base) // 1000}k tokens), "
              f"{a.seeds} seeds, ~{CHARS_PER_TOKEN} chars/token\n")
        for kind in ("disjoint", "hot", "growing"):
            print(f"### workload: {kind}")
            print("| strategy | tokens read | tokens written | total | tool calls | retries | wrong lines in final file "
                  "| runs correct | time |")
            print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
            for name, strat in strategies.items():
                agg = Counter()
                for seed in range(a.seeds):
                    rng = random.Random(seed)
                    editable = range(BODY_LINES - 1)
                    if kind == "disjoint":
                        own = [units[i::n_agents] for i in range(n_agents)]
                        tasks = [[edit_task(rng.choice(own[i]), j, i) for j in rng.sample(editable, EDITS_DISJOINT)]
                                 for i in range(n_agents)]
                    elif kind == "hot":
                        hot = [rng.choice([u for u in units if not u.top]), rng.choice(tops)]
                        per_unit = EDITS_HOT // len(hot)
                        tasks = [[edit_task(hot[k % len(hot)], per_unit * i + k // len(hot), i) for k in range(EDITS_HOT)]
                                 for i in range(n_agents)]
                    else:
                        own = [tops[i::n_agents] for i in range(n_agents)]
                        tasks = []
                        for i in range(n_agents):
                            ts = [edit_task(rng.choice(own[i]), j, i) for j in rng.sample(editable, EDITS_GROWING)]
                            for k in range(INSERTS_GROWING):
                                code = (f"def new_a{i}_{k}(x):\n" + "".join(f"    w_a{i}_{k}_{j} = {j}\n" for j in range(INSERT_BODY_LINES))
                                        + "    return x\n\n\n")
                                tail = own[i][k].lines[-1] + "\n    return x\n\n\n"
                                ts.append(Task(own[i][k], tail, tail + code, "", "", insert=code))
                            rng.shuffle(ts)
                            tasks.append(ts)
                    exp = Counter(base.splitlines())
                    for t in (t for ts in tasks for t in ts):
                        exp.subtract(t.old.splitlines())
                        exp.update(t.new.splitlines())
                    exp = +exp

                    fs, ctx, text_now = FS(base), {}, None
                    if name in mods:
                        mod, ctx["region_for"] = mods[name]
                        p = os.path.join(tempfile.mkdtemp(), "shared.py")
                        Path(p).write_text(base)
                        if hasattr(mod, "LOCK_DIR"):
                            def session(mod=mod, p=p):
                                store = mod.Store(os.path.dirname(p))

                                def call(op, *args):
                                    text = store.call(p, op, *args)
                                    head = text.split("\n", 1)[0]
                                    info = {"ok": head.startswith("ok"), "conflict": head.startswith("conflict")}
                                    if op == "read" and "\t" in head:
                                        info["h"] = head.split("\t")[2]
                                    elif "h=" in head:
                                        info["h"] = head.split("h=")[1].split()[0]
                                    return text, info
                                return call
                            text_now = Path(p).read_text
                        else:
                            doc = mod.Doc(Path(p))

                            def session(doc=doc):
                                def call(op, *args):
                                    r = getattr(doc, op)(*args)
                                    return (r, {}) if isinstance(r, str) else (json.dumps(r, indent=FASTMCP_JSON_INDENT, ensure_ascii=False), r)
                                return call
                            text_now = lambda doc=doc: doc.text  # noqa: E731
                        ctx["session"] = session
                    agents = [Agent() for _ in tasks]
                    gens = [strat(fs, agents[i], tasks[i], random.Random(seed * 1000 + i), ctx) for i in range(len(tasks))]
                    q = [(rng.uniform(0, START_JITTER_SECONDS), i, i) for i in range(len(gens))]
                    heapq.heapify(q)
                    seq, now, lock_owner, waiters = len(gens), 0.0, None, []
                    while q:
                        now, _, i = heapq.heappop(q)
                        try:
                            ev = next(gens[i])
                        except StopIteration:
                            continue
                        seq += 1
                        if ev[0] == "think":
                            heapq.heappush(q, (now + ev[1], seq, i))
                        elif ev[0] == "lock":
                            if lock_owner is None:
                                lock_owner = i
                                heapq.heappush(q, (now, seq, i))
                            else:
                                waiters.append(i)
                        elif ev[0] == "unlock":
                            lock_owner = waiters.pop(0) if waiters else None
                            if lock_owner is not None:
                                heapq.heappush(q, (now, seq, lock_owner))
                            seq += 1
                            heapq.heappush(q, (now, seq, i))
                    got = Counter((text_now() if text_now else fs.text).splitlines())
                    bad = sum(((exp - got) + (got - exp)).values())
                    agg.update(tin=sum(x.tin for x in agents), tout=sum(x.tout for x in agents),
                               calls=sum(x.calls for x in agents), retries=sum(x.retries for x in agents),
                               bad=bad, ok=int(bad == 0), t=now)
                r = {k: agg[k] / a.seeds for k in ("tin", "tout", "calls", "retries", "bad", "ok", "t")}
                print(f"| {name} | {r['tin']:,.0f} | {r['tout']:,.0f} | {r['tin'] + r['tout']:,.0f} | {r['calls']:.0f} | "
                      f"{r['retries']:.1f} | {r['bad']:.1f} | {r['ok']:.0%} | {r['t']:.1f} |")
            print()

        print("Per-session fixed cost (tool schemas + server instructions):")
        for name, (mod, _) in mods.items():
            n = tool_schema_tokens(mod)
            if n:
                print(f"- {name}: ~{n} tokens")

    if not a.skip_real:
        n = a.edits
        print(f"\nPart 2: {n_agents} real processes x {n} edits each, own line per process, one file\n")
        print("| strategy | edits on disk | lost | rebased | p50 / p99 latency |")
        print("|---|---:|---:|---:|---:|")
        ctx = mp.get_context("spawn")
        own = "".join(f"def f{k}():\n    line_{k} = 0\n\n\n" for k in range(n_agents))
        same = "def f():\n" + "".join(f"    line_{k} = 0\n" for k in range(n_agents))
        runs = [("read-modify-write", own, _rmw_worker, False, n_agents),
                ("read-modify-write + atomic rename", own, _rmw_worker, True, n_agents),
                ("region-edit, 1 session (no contention)", own, _mcp_worker, None, 1),
                ("region-edit, own region each", own, _mcp_worker, None, n_agents),
                ("region-edit, all in one region", same, _mcp_worker, "f", n_agents)]
        for label, src, worker, opt, procs in runs:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "shared.py")
                Path(p).write_text(src)
                out = ctx.Manager().list()
                b = ctx.Barrier(procs)
                ps = [ctx.Process(target=worker, args=(p, d, opt or f"f{k}", k, n, b, out)) if worker is _mcp_worker
                      else ctx.Process(target=worker, args=(p, k, n, opt, b)) for k in range(procs)]
                for x in ps:
                    x.start()
                for x in ps:
                    x.join()
                text, want = Path(p).read_text(), procs * n
                done = _count(text)
                damaged = "" if text.count("line_") == src.count("line_") else " (file damaged)"
                leftovers = len(list(Path(d).glob(".shared.py.*")))
                if worker is _mcp_worker:
                    ls = sorted(x for lat, _ in out for x in lat)
                    stats = (f"{sum(r for _, r in out)} | "
                             f"{ls[len(ls) // 2] * MS_PER_S:.1f} / {ls[int(len(ls) * P99)] * MS_PER_S:.1f} ms")
                else:
                    stats = "- | -"
                print(f"| {label} | {done} / {want}{damaged} | {want - done} | {stats} |"
                      + (f" {leftovers} temp files left!" if leftovers else ""))


if __name__ == "__main__":
    main()
