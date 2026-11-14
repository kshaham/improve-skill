"""Languages besides Python in the code layer (review 2026-11-12, section 6): JavaScript and
TypeScript resolution (tsconfig paths, workspaces, barrels, named-import calls, comments and
strings ignored), imports for Go, Java/Kotlin, Ruby and C/C++, generated and vendored code,
and coverage reporting so an unindexed language never reads as "nothing depends on this"."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import ROOT  # noqa: E402
from test_code_resolution import CodeRepoCase  # noqa: E402
import improve_code as code  # noqa: E402


class ScriptTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build({
            "tsconfig.base.json": '{\n  // shared options\n  "compilerOptions": {"baseUrl": ".", '
                                  '"paths": {"@/*": ["web/*"],}, },\n}\n',
            "tsconfig.json": '{"extends": "./tsconfig.base.json", /* the app */ "compilerOptions": {"strict": true}}',
            "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"]}),
            "packages/ui/package.json": json.dumps({"name": "@acme/ui", "main": "src/index.ts"}),
            "packages/ui/src/index.ts": "export function Button() { return 1; }\n",
            "web/a.ts": '''
                import { b as renamed } from './b';
                import { c } from '@/lib/c';
                import * as tools from './tools';
                import Default from './default';
                import { Button } from '@acme/ui';
                import { d } from './barrel';
                export { d } from './d';
                export function a() {
                  renamed();
                  tools.helper();
                  Default();
                  // c() inside a comment is not a call
                  const text = "c() in a string is not a call either";
                  return Button() + d();
                }
                const u = 'http://x'; // NOTE: url on the same line
                ''',
            "web/b.ts": "export function b() { return 1; }\n",
            "web/tools.ts": "export function helper() { return 1; }\n",
            "web/default.ts": "export default function render() { return 1; }\n",
            "web/d.ts": "export const d = () => 1;\n",
            "web/barrel.ts": "export * from './inner';\n",
            "web/inner.ts": "export { d } from './d';\n",
            "web/lib/c.ts": "export function c() {}\n",
            "web/shadow.ts": "import { b } from './b';\nexport function f(b) { return b(); }\n",
            "web/base_url.ts": "import { helper } from 'web/tools';\nexport function go() { return helper(); }\n",
            "web/legacy.js": "const tools = require('./tools');\nconst { b: bee } = require('./b');\n"
                             "function run() { return tools.helper() + bee(); }\n",
            "web/fake.ts": "// import { b } from './b';\nconst s = \"import x from './c'\";\n",
        })

    def test_should_resolve_aliases_workspaces_and_barrels(self):
        imports = {dst for (src, rel, dst) in self.edges(self.con, "imports") if src == "file:web/a.ts"}
        for target in ("web/b.ts", "web/lib/c.ts", "web/tools.ts", "web/default.ts", "packages/ui/src/index.ts",
                       "web/barrel.ts", "web/d.ts"):
            self.assertIn("file:" + target, imports)

    def test_should_link_calls_made_through_imports(self):
        targets = self.targets(self.con, "web/a.ts::a")
        self.assertEqual(targets, {"web/b.ts::b", "web/tools.ts::helper", "web/default.ts::render",
                                   "packages/ui/src/index.ts::Button", "web/d.ts::d"})
        self.assertIn(("file:web/a.ts", "uses", "web/d.ts::d"), self.edges(self.con, "uses"),
                      "two barrels deep: export * then export {d} from")

    def test_should_resolve_through_base_url(self):
        self.assertEqual(self.targets(self.con, "web/base_url.ts::go"), {"web/tools.ts::helper"})

    def test_should_link_calls_through_require(self):
        self.assertEqual(self.targets(self.con, "web/legacy.js::run"), {"web/tools.ts::helper", "web/b.ts::b"})

    def test_should_not_link_a_shadowed_alias_or_scan_comments_and_strings(self):
        self.assertEqual(self.targets(self.con, "web/shadow.ts::f"), set())
        self.assertEqual(self.targets(self.con, "file:web/fake.ts"), set())

    def test_should_keep_a_note_that_shares_its_line_with_a_url(self):
        notes = code.notes_for(self.con, ["web/a.ts"])
        self.assertEqual([(row["tag"], row["text"]) for row in notes], [("NOTE", "url on the same line")])

    def test_should_record_body_spans(self):
        symbols = {row["id"]: row for row in code.symbols_of(self.con, "web/a.ts")}
        self.assertEqual((symbols["web/a.ts::a"]["line"], symbols["web/a.ts::a"]["end_line"]), (8, 15))


class ImportsOnlyTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build({
            "go.mod": "module example.com/shop\n\ngo 1.22\n",
            "cmd/main.go": 'package main\n\nimport (\n\t"fmt"\n\tstore "example.com/shop/internal/store"\n)\n',
            "internal/store/store.go": "package store\n",
            "internal/store/cache.go": 'package store\n\nimport "example.com/shop/internal/util"\n',
            "internal/store/store_test.go": "package store\n",
            "internal/util/util.go": "package util\n",
            "src/main/java/com/acme/App.java": "package com.acme;\n\nimport com.acme.model.User;\nimport static "
                                               "com.acme.util.Strings.trim;\nimport com.acme.model.*;\n\npublic class "
                                               "App {}\n",
            "src/main/java/com/acme/model/User.java": "package com.acme.model;\n\npublic class User {}\n",
            "src/main/java/com/acme/model/Role.java": "package com.acme.model;\n\npublic enum Role {}\n",
            "src/main/java/com/acme/util/Strings.java": "package com.acme.util;\n\npublic final class Strings {}\n",
            "src/main/kotlin/com/acme/Service.kt": "package com.acme\n\nimport com.acme.model.User as Person\n\n"
                                                   "class Service\n",
            "lib/shop.rb": "require_relative 'shop/cart'\nrequire 'json'\n",
            "lib/shop/cart.rb": "class Cart\nend\n",
            "native/src/main.c": '#include "util.h"\n#include "api.h"\n#include <stdio.h>\n',
            "native/src/util.h": "int util(void);\n",
            "native/include/api.h": "int api(void);\n",
            "native/other/x.c": '#include "dup.h"\n',
            "one/include/dup.h": "",
            "two/include/dup.h": "",
        })

    def imports(self, path):
        return {dst[len("file:"):] for (src, rel, dst) in self.edges(self.con, "imports") if src == "file:" + path}

    def test_go_packages_map_through_the_module_path(self):
        self.assertEqual(self.imports("cmd/main.go"), {"internal/store/store.go", "internal/store/cache.go"})
        self.assertEqual(self.imports("internal/store/cache.go"), {"internal/util/util.go"})

    def test_java_and_kotlin_imports_map_to_files(self):
        self.assertEqual(self.imports("src/main/java/com/acme/App.java"),
                         {"src/main/java/com/acme/model/User.java", "src/main/java/com/acme/model/Role.java",
                          "src/main/java/com/acme/util/Strings.java"})
        self.assertEqual(self.imports("src/main/kotlin/com/acme/Service.kt"), {"src/main/java/com/acme/model/User.java"})

    def test_ruby_require_relative(self):
        self.assertEqual(self.imports("lib/shop.rb"), {"lib/shop/cart.rb"})

    def test_c_includes_relative_then_include_roots_and_never_ambiguous(self):
        self.assertEqual(self.imports("native/src/main.c"), {"native/src/util.h", "native/include/api.h"})
        self.assertEqual(self.imports("native/other/x.c"), set())


class GeneratedAndCoverageTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build({
            "pkg/__init__.py": "",
            "pkg/messages_pb2.py": "# Generated by the protocol buffer compiler.  DO NOT EDIT!\n"
                                   "from pkg.real import helper\nclass Message:\n    def run(self):\n"
                                   "        return helper()\n",
            "pkg/schema.py": "# Code generated by schemagen. DO NOT EDIT.\nclass Schema:\n    pass\n",
            "pkg/real.py": "def helper():\n    return 1\n",
            "pkg/user.py": "from pkg import messages_pb2\n\ndef go():\n    return messages_pb2.Message().run()\n",
            "migrations/0001_initial.py": "revision = 'a1'\ndown_revision = None\n\ndef upgrade():\n"
                                          "    helper()\n",
            "node_modules/left/index.js": "export function pad() {}\n",
            "web/vendor/lib.js": "export function x() {}\n",
            "web/app.min.js": "function a(){}\n",
            "web/app.js": "export function main() { return 1; }\n",
            "native/lib.rs": "fn main() {}\n",
            "scripts/run.sh": "echo hi\n",
        })

    def test_generated_files_are_import_targets_without_their_own_calls(self):
        self.assertIn(("file:pkg/user.py", "imports", "file:pkg/messages_pb2.py"), self.edges(self.con, "imports"))
        self.assertEqual(code.symbols_of(self.con, "pkg/messages_pb2.py"), [])
        self.assertFalse(any(src.startswith(("pkg/messages_pb2.py", "file:pkg/messages_pb2.py", "migrations/"))
                             for src, _, _ in self.edges(self.con)))
        self.assertTrue(code.file_state(self.con, "pkg/schema.py")["generated"])
        self.assertEqual(code.find(self.con, "Schema"), [], "generated classes are not search targets")

    def test_vendored_and_minified_code_is_skipped_and_counted(self):
        indexed = {row[0] for row in self.con.execute("SELECT path FROM code_file")}
        self.assertFalse(indexed & {"node_modules/left/index.js", "web/vendor/lib.js", "web/app.min.js"})
        self.assertIn("web/app.js", indexed)
        coverage = code.stats(self.con)["coverage"]
        self.assertEqual(coverage["vendored"], 3)
        self.assertEqual(coverage["not_indexed"], {"rust": 1, "shell": 1})
        self.assertEqual(coverage["languages"]["python"]["generated"], 3)
        line = code.coverage_line(self.con)
        self.assertIn("not indexed (unsupported): rust 1, shell 1", line)

    def test_cli_names_an_unindexed_language_instead_of_saying_not_found(self):
        cli = [sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo)]
        self.assertEqual(subprocess.run(cli + ["code", "index"], capture_output=True, text=True, timeout=60).returncode, 0)
        result = subprocess.run(cli + ["impact", "native/lib.rs"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 5)
        self.assertIn("rust", result.stderr)
        self.assertIn("unknown, not zero", result.stderr)
        answer = json.loads(subprocess.run(cli + ["impact", "pkg/real.py", "--format", "json"], capture_output=True,
                                           text=True, timeout=60).stdout)
        self.assertIn("rust 1", answer["coverage"], "an answer says what the code layer cannot see")


if __name__ == "__main__":
    unittest.main()
