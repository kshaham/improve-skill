"""Languages other than Python for the code layer. Standard library only.

Python is parsed with `ast` in improve_code.py. Everything here is read with conservative
regular expressions over source whose comments are removed and whose string contents are
blanked first, so a call or an import inside a string or a comment is never mistaken for code:

- JavaScript/TypeScript: top-level declarations with their body span (brace matching), every
  import form (`import`, `export ... from`, `require`, dynamic `import()`), and calls made
  through a named, default, or namespace import. Specifiers resolve relatively, through
  tsconfig/jsconfig `paths` and `baseUrl` (JSONC, `extends` followed), and through package.json
  `workspaces` package names.
- Go (`go.mod` module path -> package directory), Java/Kotlin (`package` + `import a.b.C`),
  Ruby `require_relative`, C/C++ quoted `#include`: imports only.

Every resolution is exact or refused: a specifier that matches nothing, or more than one
file where one is required, produces no edge.
"""

import bisect
import fnmatch
import json
import posixpath
import re

SCRIPT_LANGUAGES = {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
                    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript"}
OTHER_LANGUAGES = {".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".rb": "ruby",
                   ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
                   ".hxx": "cpp"}
LANGUAGES = {**SCRIPT_LANGUAGES, **OTHER_LANGUAGES}
# Import style per language: how code_import rows of that language are resolved.
STYLE = {"javascript": "js", "typescript": "js", "go": "go", "java": "jvm", "kotlin": "jvm", "ruby": "rb",
         "c": "c", "cpp": "c"}
SCRIPT_PROBES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", "/index.ts", "/index.tsx",
                 "/index.js", "/index.jsx", "/index.mjs")
# ESM TypeScript writes `./x.js` for the file `x.ts`.
TS_FOR_JS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
MAX_EXTENDS = 5
MAX_PACKAGE_FILES = 200
MAX_PACKAGE_EDGES = 30
MAX_HOPS = 8
CONFIG_NAMES = ("tsconfig.json", "jsconfig.json", "package.json", "go.mod")

# Comments, then string/template literals. Regex literals are not recognised: a `/` that
# starts one rarely contains a quote or `//`, and a mistake only hides code, never invents it.
C_LIKE_TOKEN = re.compile(r"""//[^\n]*|/\*.*?(?:\*/|\Z)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?|`(?:\\.|[^`\\])*`?""",
                          re.S)
RUBY_TOKEN = re.compile(r"""#[^\n]*|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?""")
IDENTIFIER = r"[A-Za-z_$][\w$]*"
JS_DECL = re.compile(r"^(export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?(function\*?|class)\s+("
                     + IDENTIFIER + ")", re.M)
JS_CONST_FN = re.compile(r"^(export\s+)?(?:const|let|var)\s+(" + IDENTIFIER + r")\s*(?::[^=\n]+)?=\s*(?:async\s*)?"
                         r"(?:\([^()]*(?:\([^()]*\)[^()]*)*\)|" + IDENTIFIER + r")\s*(?::\s*[^=\n]+)?=>", re.M)
JS_IMPORT_FROM = re.compile(r"\bimport\s+(type\s+)?([\w$*{}\s,]+?)\s+from\s*(['\"])([^'\"\n]+)\3")
JS_IMPORT_BARE = re.compile(r"\bimport\s*(['\"])([^'\"\n]+)\1")
JS_EXPORT_FROM = re.compile(r"\bexport\s+(type\s+)?(\*(?:\s+as\s+" + IDENTIFIER + r")?|\{[^}]*\})\s*from\s*(['\"])"
                            r"([^'\"\n]+)\3")
JS_EXPORT_LOCAL = re.compile(r"\bexport\s*\{([^}]*)\}(?!\s*from)")
JS_DYNAMIC = re.compile(r"\bimport\s*\(\s*(['\"])([^'\"\n]+)\1\s*\)")
JS_REQUIRE = re.compile(r"(?:\b(?:const|let|var)\s+(" + IDENTIFIER + r"|\{[^}]*\})\s*=\s*)?\brequire\s*\(\s*(['\"])"
                        r"([^'\"\n]+)\2\s*\)")
JS_REQUIRE_BINDING = re.compile(r"\b(?:const|let|var)\s+(?:" + IDENTIFIER + r"|\{[^}]*\})\s*=\s*require\s*\(")
JS_DECLARED = re.compile(r"\b(?:const|let|var|function\*?|class)\s+(" + IDENTIFIER + ")")
JS_PARAMS = re.compile(r"(?:\bfunction\b\s*\*?\s*(?:" + IDENTIFIER + r")?\s*\(([^()]*)\)|\(([^()]*)\)\s*(?::[^=]+?)?=>|("
                       + IDENTIFIER + r")\s*=>)")
GO_IMPORT_LINE = re.compile(r"^\s*import\s+(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)
GO_IMPORT_BLOCK = re.compile(r"^\s*import\s*\((.*?)\)", re.M | re.S)
GO_BLOCK_ENTRY = re.compile(r"^\s*(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)
GO_MODULE = re.compile(r"^\s*module\s+(\S+)", re.M)
JVM_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
JVM_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*(?:as\s+\w+)?\s*;?\s*$", re.M)
JVM_TYPE = re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|final|open|sealed|data|enum|annotation|"
                      r"inner|static)\s+)*(class|interface|enum|object|record)\s+([A-Za-z_]\w*)", re.M)
RUBY_REQUIRE = re.compile(r"\brequire_relative\s*\(?\s*['\"]([^'\"]+)['\"]")
C_INCLUDE = re.compile(r"^\s*#\s*include\s*\"([^\"]+)\"", re.M)
WHITESPACE = re.compile(r"\s*")


def language_of(path):
    """The language name for a path this module can read, or None."""
    return LANGUAGES.get(posixpath.splitext(path)[1].lower())


# ---------------------------------------------------------------- lexing

class Lexed:
    """Source split into code and comments, offsets preserved. `code` has comments removed and
    string contents blanked (quotes kept); `text` has only the comments removed, so import
    specifiers can still be read from it."""

    def __init__(self, source, ruby=False):
        pattern = RUBY_TOKEN if ruby else C_LIKE_TOKEN
        code, text, comments = [], [], []
        last = 0
        for match in pattern.finditer(source):
            start, end = match.span()
            code.append(source[last:start])
            text.append(source[last:start])
            token = match.group(0)
            if token[0] in "/#":
                comments.append((start, token))
                filler = blank(token)
                code.append(filler)
                text.append(filler)
            else:
                code.append(token[0] + blank(token[1:-1]) + token[-1] if len(token) > 1 else token)
                text.append(token)
            last = end
        code.append(source[last:])
        text.append(source[last:])
        self.source = source
        self.code = "".join(code)
        self.text = "".join(text)
        self.comments = comments
        self.newlines = [match.start() for match in re.finditer("\n", source)]

    def line(self, offset):
        """1-based line number of a character offset."""
        return bisect.bisect_left(self.newlines, offset) + 1

    def in_code(self, offset):
        """False when the offset falls inside a string or a comment."""
        return self.code[offset:offset + 1] == self.source[offset:offset + 1] and not self.code[offset:offset + 1].isspace()


def blank(text):
    return re.sub(r"[^\n]", " ", text)


def comment_notes(lexed, pattern):
    """(line, tag, text) for every note comment; each line of a block comment is its own candidate,
    so a note sharing a line with a URL inside a string is still found."""
    notes = []
    for start, token in lexed.comments:
        first = lexed.line(start)
        for offset, line in enumerate(token.split("\n")):
            match = pattern.match(line)
            if match:
                notes.append((first + offset, match.group(1), match.group(2)))
    return notes


def matching_brace(code, start):
    """Offset of the brace closing the one at `start`, or None."""
    depth = 0
    for index in range(start, len(code)):
        char = code[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def skip_parentheses(code, start):
    """Offset just past the balanced parenthesis group that starts at or after `start`."""
    opening = code.find("(", start)
    if opening < 0:
        return start
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == "(":
            depth += 1
        elif code[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(code)


# ---------------------------------------------------------------- JavaScript and TypeScript

def extract_script(path, source, note_pattern):
    """Symbols (with body spans), import rows, named-import calls, and note comments."""
    lexed = Lexed(source)
    symbols, spans = script_symbols(path, lexed)
    imports = script_imports(lexed)
    calls = script_calls(path, lexed, imports, spans)
    rows = [(alias, module, name, -1, line, deferred, "", "js", export)
            for alias, module, name, line, deferred, export in imports]
    return {"symbols": symbols, "imports": rows, "calls": calls,
            "notes": comment_notes(lexed, note_pattern)}


def script_symbols(path, lexed):
    code = lexed.code
    symbols = {f"{path}::": (f"{path}::", "", "", "module", 1, None, None, None, None)}
    spans = []
    for match in JS_DECL.finditer(code):
        name, kind = match.group(3), "class" if match.group(2) == "class" else "function"
        body = code.find("{", skip_parentheses(code, match.end()) if kind == "function" else match.end())
        end = matching_brace(code, body) if body >= 0 else None
        add_script_symbol(path, lexed, symbols, spans, name, kind, match.start(), end)
    for match in JS_CONST_FN.finditer(code):
        body = WHITESPACE.match(code, match.end()).end()
        if code[body:body + 1] == "{":
            end = matching_brace(code, body)
        else:
            end = code.find("\n", body)
            end = len(code) - 1 if end < 0 else end
        add_script_symbol(path, lexed, symbols, spans, match.group(2), "function", match.start(), end)
    return list(symbols.values()), spans


def add_script_symbol(path, lexed, symbols, spans, name, kind, start, end):
    symbol_id = f"{path}::{name}"
    if symbol_id in symbols:
        return
    end_line = lexed.line(end) if end is not None else None
    symbols[symbol_id] = (symbol_id, name, name, kind, lexed.line(start), end_line, None, None, None)
    if end is not None:
        spans.append((start, end, symbol_id))


def script_imports(lexed):
    """(alias, specifier or None, imported name, line, deferred, export) for every import form."""
    text, rows = lexed.text, []

    def live(match):
        return lexed.in_code(match.start())

    for match in JS_IMPORT_FROM.finditer(text):
        if live(match):
            line, deferred = lexed.line(match.start()), int(bool(match.group(1)))
            rows.extend((alias, match.group(4), name, line, deferred, 0) for alias, name in import_clause(match.group(2)))
    for match in JS_IMPORT_BARE.finditer(text):
        if live(match):
            rows.append(("", match.group(2), None, lexed.line(match.start()), 0, 0))
    for match in JS_EXPORT_FROM.finditer(text):
        if live(match):
            line, deferred, clause = lexed.line(match.start()), int(bool(match.group(1))), match.group(2)
            if clause.startswith("*"):
                alias = clause.split()[-1] if " as " in f" {clause} " else "*"
                rows.append((alias, match.group(4), "*", line, deferred, 1))
            else:
                rows.extend((alias, match.group(4), name, line, deferred, 1) for alias, name in named_list(clause[1:-1]))
    for match in JS_EXPORT_LOCAL.finditer(text):
        if live(match):
            rows.extend((alias, None, name, lexed.line(match.start()), 0, 1) for alias, name in named_list(match.group(1)))
    for match in JS_DECL.finditer(lexed.code):
        if match.group(1) and "default" in match.group(1):
            rows.append(("default", None, match.group(3), lexed.line(match.start()), 0, 1))
    for match in JS_DYNAMIC.finditer(text):
        if live(match):
            rows.append(("", match.group(2), None, lexed.line(match.start()), 1, 0))
    for match in JS_REQUIRE.finditer(text):
        if not live(match):
            continue
        line, target = lexed.line(match.start()), match.group(1)
        if target and target.startswith("{"):
            rows.extend((alias, match.group(3), name, line, 0, 0)
                        for alias, name in named_list(target[1:-1], separator=":"))
        elif target:
            rows.append((target, match.group(3), "*", line, 0, 0))
        else:
            rows.append(("", match.group(3), None, line, 0, 0))
    return rows


def import_clause(clause):
    """`a`, `{b as c}`, `* as ns`, or `a, {b}` -> [(local alias, imported name)]."""
    clause = clause.strip()
    found = []
    braces = re.search(r"\{([^}]*)\}", clause)
    if braces:
        found.extend(named_list(braces.group(1)))
        clause = (clause[:braces.start()] + clause[braces.end():]).strip(" ,")
    star = re.search(r"\*\s*as\s+(" + IDENTIFIER + ")", clause)
    if star:
        found.append((star.group(1), "*"))
        clause = (clause[:star.start()] + clause[star.end():]).strip(" ,")
    if re.fullmatch(IDENTIFIER, clause or "-"):
        found.append((clause, "default"))
    return found


def named_list(body, separator="as"):
    pairs = []
    for item in body.split(","):
        item = re.sub(r"^\s*type\s+", "", item).strip()
        if not item:
            continue
        if separator == ":":
            parts = [part.strip() for part in item.split(":", 1)]
            original, alias = parts[0], parts[-1].split("=")[0].strip()
        else:
            parts = re.split(r"\s+as\s+", item)
            original, alias = parts[0].strip(), parts[-1].strip()
        if re.fullmatch(IDENTIFIER, original) and re.fullmatch(IDENTIFIER, alias):
            pairs.append((alias, original))
    return pairs


def shadowed_names(code):
    """Every name the file declares or takes as a parameter: an import alias reused that way may
    not be the import at a call site, so calls through it are not linked. A `require` binding is
    the import itself, not a second declaration."""
    code = JS_REQUIRE_BINDING.sub(lambda match: blank(match.group(0)), code)
    names = set(JS_DECLARED.findall(code))
    for match in JS_PARAMS.finditer(code):
        params = match.group(1) or match.group(2) or match.group(3) or ""
        names.update(re.findall(IDENTIFIER, re.sub(r"=[^,]*", "", re.sub(r":[^,]*", "", params))))
    return names


def script_calls(path, lexed, imports, spans):
    """Calls through imports: `alias(`, `new alias(`, and `ns.member(`; never a local name."""
    named, namespaces = {}, {}
    for alias, module, name, _, deferred, export in imports:
        if export or not alias or not module or deferred:
            continue
        (namespaces if name == "*" else named)[alias] = name
    shadowed = shadowed_names(lexed.code)
    declared_once = {alias for alias in named if alias not in shadowed}
    spaces = {alias for alias in namespaces if alias not in shadowed}
    if not declared_once and not spaces:
        return []
    names = "|".join(re.escape(alias) for alias in sorted(declared_once | spaces, key=len, reverse=True))
    pattern = re.compile(r"(?<![\w$.])(" + names + r")\s*(?:\.\s*(" + IDENTIFIER + r"))?\s*\(")
    spans = sorted(spans)
    starts = [span[0] for span in spans]
    calls = []
    for match in pattern.finditer(lexed.code):
        alias, member = match.group(1), match.group(2)
        if alias in spaces and not member:
            continue
        if alias in declared_once and member:
            continue
        caller = enclosing(spans, starts, match.start()) or f"{path}::"
        calls.append((caller, "calls", "import", "", alias, member, None, lexed.line(match.start())))
    return calls


def enclosing(spans, starts, offset):
    """The innermost declaration whose body contains the offset."""
    index = bisect.bisect_right(starts, offset) - 1
    best = None
    while index >= 0:
        start, end, symbol_id = spans[index]
        if start <= offset <= end:
            best = symbol_id
            break
        index -= 1
    return best


# ---------------------------------------------------------------- imports-only languages

def extract_other(lang, path, source, note_pattern):
    """Import rows (and, for Java/Kotlin, declared type names) for an imports-only language."""
    ruby = lang == "ruby"
    lexed = Lexed(source, ruby=ruby)
    style = STYLE[lang]
    symbols = [(f"{path}::", "", "", "module", 1, None, None, None, None)]
    imports, module = [], None
    if lang == "go":
        found = [(match.start(1), match.group(1)) for match in GO_IMPORT_LINE.finditer(lexed.text)]
        for block in GO_IMPORT_BLOCK.finditer(lexed.text):
            found.extend((block.start(1) + entry.start(1), entry.group(1))
                         for entry in GO_BLOCK_ENTRY.finditer(block.group(1)))
        imports = [("", spec, None, 0, lexed.line(offset), 0, "", style, 0) for offset, spec in sorted(set(found))]
    elif style == "jvm":
        package = JVM_PACKAGE.search(lexed.code)
        module = package.group(1) if package else ""
        for match in JVM_IMPORT.finditer(lexed.code):
            name = "*" if match.group(3) else None
            imports.append(("", match.group(2), name, 0, lexed.line(match.start()), 0, "", style, 0))
        for match in JVM_TYPE.finditer(lexed.code):
            symbol_id = f"{path}::{match.group(2)}"
            symbols.append((symbol_id, match.group(2), match.group(2), "class", lexed.line(match.start(2)), None, None,
                            None, None))
    elif ruby:
        imports = [("", match.group(1), None, 0, lexed.line(match.start()), 0, "", style, 0)
                   for match in RUBY_REQUIRE.finditer(lexed.text) if lexed.in_code(match.start())]
    else:
        imports = [("", match.group(1), None, 0, lexed.line(match.start()), 0, "", style, 0)
                   for match in C_INCLUDE.finditer(lexed.text)]
    unique = list({row[0]: row for row in symbols}.values())
    return {"symbols": unique, "imports": imports, "calls": [], "notes": comment_notes(lexed, note_pattern),
            "module": module}


# ---------------------------------------------------------------- project configuration

def load_config(repo, listing):
    """What resolution needs from tsconfig/jsconfig, package.json workspaces, and go.mod, as
    plain JSON-serialisable data (stored, so a config change triggers re-resolution)."""
    configs = [path for path in listing if posixpath.basename(path) in CONFIG_NAMES]
    result = {}
    ts = []
    for path in configs:
        if posixpath.basename(path) in ("tsconfig.json", "jsconfig.json"):
            entry = tsconfig_entry(repo, path)
            if entry:
                ts.append(entry)
    if ts:
        result["ts"] = sorted(ts, key=lambda entry: entry["dir"])
    workspaces = workspace_packages(repo, [path for path in configs if posixpath.basename(path) == "package.json"])
    if workspaces:
        result["workspaces"] = workspaces
    go = {}
    for path in configs:
        if posixpath.basename(path) == "go.mod":
            text = read_text(repo, path)
            match = GO_MODULE.search(text or "")
            if match:
                go[posixpath.dirname(path)] = match.group(1)
    if go:
        result["go"] = go
    return result


def read_text(repo, path, limit=1_000_000):
    target = repo / path
    try:
        if target.is_symlink() or not target.is_file() or target.stat().st_size > limit:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_jsonc(text):
    """JSON with comments and trailing commas (tsconfig's dialect), or None."""
    if text is None:
        return None
    lexed = Lexed(text)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", lexed.text)
    try:
        value = json.loads(cleaned)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def inside_repo(path):
    normal = posixpath.normpath(path)
    return None if normal.startswith("../") or normal == ".." or normal.startswith("/") else ("" if normal == "." else normal)


def tsconfig_entry(repo, path):
    """Effective `baseUrl` and `paths` of one tsconfig, following `extends` (relative only)."""
    directory = posixpath.dirname(path)
    base, paths, paths_base = None, None, None
    chain, current, seen = [], path, set()
    while current and current not in seen and len(chain) <= MAX_EXTENDS:
        seen.add(current)
        data = parse_jsonc(read_text(repo, current))
        if data is None:
            break
        chain.append((current, data))
        parent = data.get("extends")
        parent = parent[0] if isinstance(parent, list) and parent else parent
        if not isinstance(parent, str) or not parent.startswith("."):
            break
        target = inside_repo(posixpath.join(posixpath.dirname(current), parent))
        if target is None:
            break
        current = target if target.endswith(".json") else target + ".json"
    for config, data in reversed(chain):  # The extending file wins.
        options = data.get("compilerOptions") if isinstance(data.get("compilerOptions"), dict) else {}
        here = posixpath.dirname(config)
        if isinstance(options.get("baseUrl"), str):
            base = inside_repo(posixpath.join(here, options["baseUrl"]))
        if isinstance(options.get("paths"), dict):
            paths = {key: [value for value in values if isinstance(value, str)]
                     for key, values in options["paths"].items() if isinstance(values, list)}
            paths_base = here
    if base is None and paths is None:
        return None
    return {"dir": directory, "base": base, "paths": sorted((paths or {}).items()),
            "paths_base": base if base is not None else paths_base}


def workspace_packages(repo, package_files):
    """Package name -> directory for every workspace package of every workspace root."""
    manifests = {}
    for path in package_files[:MAX_PACKAGE_FILES]:
        data = parse_jsonc(read_text(repo, path))
        if data is not None:
            manifests[posixpath.dirname(path)] = data
    found = {}
    for root, data in sorted(manifests.items()):
        patterns = data.get("workspaces")
        if isinstance(patterns, dict):
            patterns = patterns.get("packages")
        if not isinstance(patterns, list):
            continue
        for directory, manifest in sorted(manifests.items()):
            relative = directory[len(root) + 1:] if root else directory
            if directory == root or (root and not directory.startswith(root + "/")):
                continue
            if any(isinstance(pattern, str) and fnmatch.fnmatch(relative, pattern.rstrip("/")) for pattern in patterns):
                name = manifest.get("name")
                if isinstance(name, str) and name:
                    entry = {"dir": directory}
                    for field in ("source", "module", "main", "types"):
                        if isinstance(manifest.get(field), str):
                            entry.setdefault("entry", inside_repo(posixpath.join(directory, manifest[field])))
                    found[name] = entry
    return found


# ---------------------------------------------------------------- resolution

class LanguageResolver:
    """Resolves import rows of the non-Python styles against the indexed files."""

    def __init__(self, files, config, jvm_types):
        self.files = files
        self.config = config or {}
        self.ts = sorted(self.config.get("ts", []), key=lambda entry: -len(entry["dir"]))
        self.workspaces = self.config.get("workspaces", {})
        self.go = self.config.get("go", {})
        self.jvm = {}
        for fqcn, path in jvm_types:
            self.jvm.setdefault(fqcn, set()).add(path)
        self.directories = {}
        for path in files:
            self.directories.setdefault(posixpath.dirname(path), []).append(path)
        self.include_roots = sorted({directory for directory in self.directories
                                     if posixpath.basename(directory) == "include"} | {""})

    def probe(self, base):
        base = inside_repo(base) if base is not None else None
        if base is None:
            return None
        for suffix in SCRIPT_PROBES:
            if base + suffix in self.files:
                return base + suffix
        stem, extension = posixpath.splitext(base)
        for replacement in TS_FOR_JS.get(extension, ()):
            if stem + replacement in self.files:
                return stem + replacement
        return None

    def script(self, importer, spec):
        """The file a JavaScript/TypeScript specifier names, or None (a package, or unknown)."""
        if not spec:
            return None
        if spec.startswith("."):
            return self.probe(posixpath.join(posixpath.dirname(importer), spec))
        config = self.tsconfig_for(importer)
        if config:
            found = self.through_paths(config, spec)
            if found:
                return found
            if config.get("base") is not None:
                found = self.probe(posixpath.join(config["base"], spec))
                if found:
                    return found
        return self.through_workspaces(spec)

    def tsconfig_for(self, importer):
        for entry in self.ts:
            if not entry["dir"] or importer.startswith(entry["dir"] + "/"):
                return entry
        return None

    def through_paths(self, config, spec):
        base = config.get("paths_base") or ""
        best = None
        for pattern, targets in config.get("paths", []):
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix):
                    middle = spec[len(prefix):len(spec) - len(suffix)]
                    if best is None or len(prefix) > best[0]:
                        best = (len(prefix), [target.replace("*", middle, 1) for target in targets])
            elif pattern == spec:
                best = (len(pattern) + 1, targets)
        for target in (best[1] if best else []):
            found = self.probe(posixpath.join(base, target))
            if found:
                return found
        return None

    def through_workspaces(self, spec):
        for name in sorted(self.workspaces, key=len, reverse=True):
            if spec == name or spec.startswith(name + "/"):
                entry = self.workspaces[name]
                directory, rest = entry["dir"], spec[len(name) + 1:]
                if rest:
                    return self.probe(posixpath.join(directory, rest)) or self.probe(posixpath.join(directory, "src", rest))
                candidates = [entry.get("entry"), posixpath.join(directory, "src/index"), posixpath.join(directory, "index")]
                for candidate in candidates:
                    if candidate is None:
                        continue
                    found = self.probe(candidate) or self.probe(posixpath.splitext(candidate)[0])
                    if found:
                        return found
                return None
        return None

    def targets(self, style, importer, spec, name):
        """Files an imports-only row names (several for a Go package or a Java wildcard)."""
        if style == "go":
            return self.go_package(importer, spec)
        if style == "jvm":
            return self.jvm_import(spec, name)
        if style == "rb":
            base = posixpath.join(posixpath.dirname(importer), spec)
            candidate = inside_repo(base if base.endswith(".rb") else base + ".rb")
            return [candidate] if candidate in self.files else []
        if style == "c":
            return self.include(importer, spec)
        return []

    def go_package(self, importer, spec):
        best = None
        for directory, module in self.go.items():
            if (spec == module or spec.startswith(module + "/")) and (best is None or len(module) > len(best[1])):
                best = (directory, module)
        if best is None:
            return []
        directory = inside_repo(posixpath.join(best[0], spec[len(best[1]) + 1:]) if spec != best[1] else best[0] or ".")
        files = [path for path in self.directories.get(directory, []) if path.endswith(".go")
                 and not path.endswith("_test.go")]
        return sorted(files)[:MAX_PACKAGE_EDGES]

    def jvm_import(self, spec, name):
        if name == "*":
            found = set()
            for fqcn, paths in self.jvm.items():
                if fqcn.rpartition(".")[0] == spec:
                    found |= paths
            return sorted(found)[:MAX_PACKAGE_EDGES]
        parts = spec.split(".")
        for size in range(len(parts), 1, -1):  # `import static a.b.C.m` names the class a.b.C
            paths = self.jvm.get(".".join(parts[:size]))
            if paths:
                return sorted(paths) if len(paths) == 1 else []
        return []

    def include(self, importer, spec):
        relative = inside_repo(posixpath.join(posixpath.dirname(importer), spec))
        if relative in self.files:
            return [relative]
        found = {candidate for candidate in (inside_repo(posixpath.join(root, spec)) for root in self.include_roots)
                 if candidate in self.files}
        return sorted(found) if len(found) == 1 else []
