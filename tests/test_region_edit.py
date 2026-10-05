import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from region_edit import Store, handle, split_regions  # noqa: E402


def ids(suffix, src):
    return [r.id for r in split_regions(suffix, src.splitlines(True))]


def test_python_methods_are_regions():
    src = (
        "import os\n\n"
        "class A:\n    x = 1\n\n    def f(self):\n        def inner():\n            pass\n\n"
        "    @property\n    def g(self):\n        return 1\n\n"
        "async def top():\n    pass\n"
    )
    assert ids(".py", src) == ["<head>", "A", "A.f", "A.g", "top"]


def test_rust_impl_members_and_closer():
    src = (
        "use std::fmt;\n\n"
        "pub struct S;\n\n"
        "impl fmt::Display for S {\n"
        "    /// doc\n    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n        Ok(())\n    }\n"
        "    pub async fn go(&self) {}\n}\n\n"
        "fn main() {}\n"
    )
    assert ids(".rs", src) == ["<head>", "struct S", "impl fmt::Display for S", "impl fmt::Display for S.fmt",
                               "impl fmt::Display for S.go", "impl fmt::Display for S.end", "fn main"]


def test_js_and_go():
    js = (
        "import x from 'x';\n\n"
        "/** doc */\nexport class C {\n  constructor() {\n    if (a) {}\n  }\n  async run() {}\n  h = () => 1;\n}\n\n"
        "export const f = (a) => a;\nconst N = 3;\nfunction g() {}\n"
    )
    assert ids(".ts", js) == ["<head>", "C", "C.constructor", "C.run", "C.h", "C.end", "f", "g"]
    go = "package m\n\n// T doc\ntype T struct{}\n\nfunc (t *T) M() {}\n\nfunc F() {}\n"
    assert ids(".go", go) == ["<head>", "T", "T.M", "F"]


def test_regions_partition_file():
    src = Path(__file__).read_text()
    lines = src.splitlines(True)
    regs = split_regions(".py", lines)
    assert regs[0].start == 0 and regs[-1].end == len(lines)
    assert all(a.end == b.start for a, b in zip(regs, regs[1:]))


def h_of(text):
    head = text.split("\n", 1)[0]
    return head.split("\t")[2] if "\t" in head else head.split("h=")[1].split()[0]


def test_crlf_preserved(tmp_path):
    p = str(tmp_path / "a.py")
    Path(p).write_bytes(b"def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n")
    s = Store(tmp_path)
    assert s.call(p, "edit", "f", h_of(s.call(p, "read", "f")), "return 1", "return 3").startswith("ok")
    assert Path(p).read_bytes() == b"def f():\r\n    return 3\r\n\r\ndef g():\r\n    return 2\r\n"


def test_rebase_conflict_and_errors(tmp_path):
    p = str(tmp_path / "a.py")
    Path(p).write_text("def f():\n    a = 1\n    b = 2\n")
    s = Store(tmp_path)
    h = h_of(s.call(p, "read", "f"))
    r1 = s.call(p, "edit", "f", h, "a = 1", "a = 10")
    assert r1.startswith("ok h=") and "rebased" not in r1
    r2 = s.call(p, "edit", "f", h, "b = 2", "b = 20")
    assert r2.endswith(" rebased")
    r3 = s.call(p, "edit", "f", h, "a = 1\n", "a = 5\n")
    assert r3.startswith("conflict h=") and "+    a = 10" in r3
    assert s.call(p, "replace_region", "f", h, "def f(): pass\n").startswith("conflict")
    assert s.call(p, "edit", "f", h_of(r2), "zzz", "y") == "error: anchor_not_found"
    assert s.call(p, "read", "nope").startswith("error: no_such_region")


def test_sessions_and_external_edits(tmp_path):
    p = str(tmp_path / "a.py")
    Path(p).write_text("def f():\n    a = 1\n    b = 2\n")
    a, b = Store(tmp_path), Store(tmp_path)
    ha, hb = h_of(a.call(p, "read", "f")), h_of(b.call(p, "read", "f"))
    assert b.call(p, "edit", "f", hb, "a = 1\n", "a = 2\n").startswith("ok")
    assert a.call(p, "edit", "f", ha, "b = 2", "b = 3").endswith("rebased")
    Path(p).write_text(Path(p).read_text().replace("b = 3", "b = 4"))
    c = a.call(p, "edit", "f", ha, "b = 3", "b = 5")
    assert c.startswith("conflict") and "+    b = 4" in c
    assert Path(p).read_text() == "def f():\n    a = 2\n    b = 4\n"
    assert not list(tmp_path.glob(".a.py.*"))


def test_text_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text('def f():\n    return "x"\n\ndef g():\n    pass\n')
    s = Store(tmp_path)
    assert s.call("a.py", "outline").startswith("f\t1-3\t")
    r = s.call("a.py", "read", ["f", "g"])
    assert r.startswith("f\t1-3\t") and 'return "x"' in r and "\ng\t4-5\t" in r
    assert s.call("a.py", "edit", "f", h_of(r), '"x"', '"y"').startswith("ok h=")
    assert "\n+k\t" in s.call("a.py", "insert", "<end>", "def k():\n    pass")
    assert s.call("new.md", "insert", "<end>", "# hi").startswith("ok")
    assert (tmp_path / "new.md").read_text() == "# hi\n"
    assert 'return "y"' in (tmp_path / "a.py").read_text()
    try:
        s.call("/etc/hosts", "outline")
        raise AssertionError
    except ValueError:
        pass


def test_mcp_handle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    s = Store(tmp_path)
    init = handle(s, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"] == {"tools": {}}
    assert handle(s, "initialize", {"protocolVersion": "1999-01-01"})["protocolVersion"] == "2025-11-25"
    names = [t["name"] for t in handle(s, "tools/list", {})["tools"]]
    assert names == ["outline", "read", "edit", "replace_region", "insert"]
    r = handle(s, "tools/call", {"name": "read", "arguments": {"path": "a.py", "region": "f"}})
    assert not r["isError"] and r["content"][0]["text"].startswith("f\t1-2\t")
    assert handle(s, "tools/call", {"name": "edit", "arguments": {"path": "a.py"}})["isError"]
    assert handle(s, "tools/call", {"name": "nope", "arguments": {"path": "a.py"}})["isError"]
