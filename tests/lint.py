"""A small standard-library lint run in CI: every Python file compiles for Python 3.9's
grammar, and no module imports a name it never uses. (The project has no dependencies, so it
does not pull in pyflakes; this catches the two problems that have actually slipped in.)

Usage: python3 tests/lint.py [paths...]   (default: scripts/ and tests/)
Exit status 1 lists every problem as path:line: message. `# noqa` on a line skips it.
"""
import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def python_files(paths):
    for path in paths:
        path = Path(path)
        if path.is_dir():
            yield from sorted(item for item in path.rglob("*.py") if "fixtures" not in item.parts)
        elif path.suffix == ".py":
            yield path


def unused_imports(tree, lines):
    """Imported names never read anywhere else in the module (re-exports via __all__ count)."""
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = (alias.asname or alias.name).split(".")[0]
                if "noqa" not in lines[node.lineno - 1]:
                    imported.setdefault(name, node.lineno)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.value.id for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
    exported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            exported |= {element.value for element in getattr(node.value, "elts", []) if isinstance(element, ast.Constant)}
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    return [(line, f"'{name}' imported but unused") for name, line in sorted(imported.items(), key=lambda item: item[1])
            if name not in used and name not in exported and name not in strings]


def unused_locals(tree, lines):
    """Local variables assigned once and never read in the same function."""
    problems = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stored, loaded = {}, set()
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                # Only plain `name = value`: unpacking and loop targets name parts on purpose.
                stored.setdefault(node.targets[0].id, node.lineno)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                loaded.add(node.id)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                loaded.update(node.names)
        for name, line in stored.items():
            if name not in loaded and not name.startswith("_") and "noqa" not in lines[line - 1]:
                problems.append((line, f"local '{name}' assigned but never used"))
    return problems


def check(path):
    source = path.read_text()
    lines = source.splitlines() + [""]
    try:
        tree = ast.parse(source, filename=str(path), feature_version=(3, 9))
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"not valid Python 3.9: {exc.msg}")]
    return unused_imports(tree, lines) + unused_locals(tree, lines)


def main(argv=None):
    paths = (argv if argv is not None else sys.argv[1:]) or [ROOT / "scripts", ROOT / "tests"]
    problems = []
    for path in python_files(paths):
        for line, message in check(path):
            problems.append(f"{path.relative_to(ROOT) if path.is_absolute() else path}:{line}: {message}")
    print("\n".join(problems) if problems else "lint: no problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
