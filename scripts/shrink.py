#!/usr/bin/env python3
"""
Score a working tree against the shrink budget.

A commit whose subject claims a cleanup (collapse/share/consolidate/unify/
centralize/trim/simplify) must reduce every number this prints. A positive
number means the subject line is wrong: either rename the commit to what it
actually is, or don't merge it. Run with no args to score uncommitted work,
or pass a git ref to score against it.
"""
import argparse, ast, datetime, io, subprocess, sys, tokenize
from pathlib import Path

SRC = ("xsettlers_mcp", "engine", "db", "views", "npc", "config", "scripts")

def prose_lines(text: str) -> int:
    """Docstring lines plus standalone comment lines in one Python source."""
    n = 0
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                n += d.count("\n") + 1
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                n += 1
    except (tokenize.TokenError, IndentationError):
        pass
    return n

def measure(read) -> dict:
    files = code = prose = docs = 0
    for path in read.paths():
        p = str(path)
        if p.endswith(".py") and p.split("/")[0] in SRC:
            text = read.text(path)
            files += 1
            code += text.count("\n") + 1
            prose += prose_lines(text)
        elif p.endswith(".md") and (p.startswith("docs/") or p == "CLAUDE.md"):
            files += 1
            docs += read.text(path).count("\n") + 1
    return {"files": files, "lines": code, "prose": prose, "docs": docs}

class Tree:
    def paths(self):
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout
        return [Path(l) for l in out.splitlines() if l]
    def text(self, p):
        try:
            return Path(p).read_text()
        except (OSError, UnicodeDecodeError):
            return ""

class Ref:
    def __init__(self, ref):
        self.ref = ref
    def paths(self):
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", self.ref],
                             capture_output=True, text=True).stdout
        return [Path(l) for l in out.splitlines() if l]
    def text(self, p):
        r = subprocess.run(["git", "show", f"{self.ref}:{p}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""

# --- fresh-session context cost -------------------------------------------
# What a new session pays before any work happens, in three tiers:
#   floor    CLAUDE.md + the auto-memory index -- injected by the harness
#   directed + what CLAUDE.md tells the agent to read first
#   ondemand + the rest of docs/, paid when a task touches them
MEMORY_INDEX = Path.home() / ".claude/projects" / \
    "-Users-vincentlowe-Documents-src-xsettlers26/memory/MEMORY.md"
DIRECTED = ("docs/TODO.md", "docs/dev_history.md")

def est_tokens(text: str) -> int:
    """
    Offline BPE approximation: each whitespace-separated chunk costs about one
    token per four characters, minimum one. Absolute values carry maybe 10-15%
    error; the before/after delta is far tighter, since both sides are the
    same kind of text.
    """
    return sum(max(1, -(-len(c) // 4)) for c in text.split())

def context_cost(read) -> dict:
    docs = [str(p) for p in read.paths()
            if str(p).endswith(".md") and str(p).startswith("docs/")]
    def total(paths):
        return sum(est_tokens(read.text(p)) for p in paths)
    floor = est_tokens(read.text("CLAUDE.md"))
    try:
        floor += est_tokens(MEMORY_INDEX.read_text())
    except OSError:
        pass
    directed = total([d for d in DIRECTED if d in docs])
    return {"floor": floor,
            "directed": floor + directed,
            "ondemand": floor + total(docs)}

def report_context(base, label):
    now, was = context_cost(Tree()), context_cost(base)
    print("fresh-session context, estimated tokens\n")
    print(f"  {'tier':10s} {'before':>8s} {'after':>8s} {'delta':>8s}")
    for k in ("floor", "directed", "ondemand"):
        print(f"  {k:10s} {was[k]:8d} {now[k]:8d} {now[k]-was[k]:+8d}")
    log = Path("scripts/context_log.tsv")
    if not log.exists():
        log.write_text("date\tlabel\tfloor\tdirected\tondemand\n")
    with log.open("a") as fh:
        fh.write(f"{datetime.date.today()}\t{label}\t"
                 f"{now['floor']}\t{now['directed']}\t{now['ondemand']}\n")
    print(f"\n  logged to {log}")

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ref", nargs="?", default="HEAD")
    ap.add_argument("--context", action="store_true",
                    help="report fresh-session context cost instead of line counts")
    ap.add_argument("--label", default="unlabeled", help="name this measurement in the log")
    args = ap.parse_args()
    base = Ref(args.ref)
    if args.context:
        return report_context(base, args.label)
    now = measure(Tree())
    was = measure(base)
    parts = []
    for k in ("files", "lines", "prose", "docs"):
        parts.append(f"{k} {now[k] - was[k]:+d}")
    print("  ".join(parts))
    for k in ("files", "lines", "prose", "docs"):
        print(f"  {k:6s} {was[k]:6d} -> {now[k]:6d}")

if __name__ == "__main__":
    main()
