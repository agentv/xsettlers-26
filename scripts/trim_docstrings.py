#!/usr/bin/env python3
"""
Drop whole paragraphs from a docstring, by AST.

The point is that no sentence is ever rewritten -- paragraphs are kept or
dropped intact, so the only insertions a diff can show are existing lines with
a closing quote moved onto them. Rewriting a docstring shorter is regeneration,
and regeneration is how prose weight comes back (see docs/dev_history.md).

    scripts/trim_docstrings.py engine/turn.py              # list paragraphs
    scripts/trim_docstrings.py engine/turn.py _resolve_scan 0 2   # keep 0 and 2
"""
import ast, sys


def _nodes(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield getattr(n, "name", "<module>"), n


def show(path, only=None):
    for name, node in _nodes(ast.parse(open(path).read())):
        if only and name != only:
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        for i, para in enumerate(doc.split("\n\n")):
            print(f"  {name:32s} [{i}] {len(para.splitlines()):2d}L  "
                  f"{' '.join(para.split())[:86]}")


def drop(path, name, keep):
    """Keep only the paragraph indices in `keep`; delete the rest."""
    lines = open(path).read().split("\n")
    node = [n for nm, n in _nodes(ast.parse("\n".join(lines)))
            if nm == name and ast.get_docstring(n, clean=False)][0]
    lit = node.body[0].value
    paras = ast.get_docstring(node, clean=False).split("\n\n")
    missing = [i for i in keep if i >= len(paras)]
    assert not missing, f"{path}:{name} has {len(paras)} paragraphs, asked for {missing}"
    quote = lines[lit.lineno - 1][lit.col_offset:lit.col_offset + 3]
    assert quote in ('"""', "'''"), f"{path}:{name} unexpected quote {quote!r}"
    head = lines[lit.lineno - 1][:lit.col_offset]
    tail = lines[lit.end_lineno - 1][lit.end_col_offset:]
    body = "\n\n".join(paras[i] for i in sorted(keep))
    lines[lit.lineno - 1:lit.end_lineno] = (head + quote + body + quote + tail).split("\n")
    open(path, "w").write("\n".join(lines))


if __name__ == "__main__":
    if len(sys.argv) == 2:
        show(sys.argv[1])
    elif len(sys.argv) == 3:
        show(sys.argv[1], sys.argv[2])
    else:
        drop(sys.argv[1], sys.argv[2], {int(a) for a in sys.argv[3:]})
