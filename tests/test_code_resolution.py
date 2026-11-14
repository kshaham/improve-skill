"""Python resolution in the code layer never guesses (review 2026-11-12, sections 3.5 and 6):
scoped bindings, builtins, star imports, package roots, re-exports, inheritance, instance /
inherited / super() calls, symbol references, overrides in impact, and every call site.
Each case from the review's probes (graphify-probes/probe_calls.py, probe_more.py,
probe_roots.py; graph-probes/p3_resolver.py) is a test here."""
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import GIT_IDENTITY, environment, make_repo, restore_environment  # noqa: E402
import improve_code as code  # noqa: E402


class CodeRepoCase(unittest.TestCase):
    """Builds a committed repository from sources and indexes it into an in-memory database."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve code resolution ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.count = 0

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def build(self, sources, commit=True):
        self.count += 1
        repo = make_repo(Path(self.temp.name) / f"repo{self.count}", []).resolve()
        self.write(repo, sources)
        self.git(repo, "add", "-A")
        if commit:
            self.git(repo, "commit", "-qm", "sources")
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        self.report = code.index(con, repo, max_seconds=0)
        return repo, con

    @staticmethod
    def write(repo, sources):
        for name, text in sources.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).lstrip("\n"))

    @staticmethod
    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, *args], check=True, capture_output=True, text=True)

    @staticmethod
    def edges(con, rel=None):
        query = "SELECT src, rel, dst, confidence, tier FROM code_edge" + (" WHERE rel=?" if rel else "")
        return {(src, kind, dst): (confidence, tier)
                for src, kind, dst, confidence, tier in con.execute(query, (rel,) if rel else ())}

    @staticmethod
    def targets(con, src):
        return {dst for (dst,) in con.execute("SELECT dst FROM code_edge WHERE src=?", (src,))}


PROBE = {
    "pkg/__init__.py": "from .svc import Service, make\n__all__ = ['Service', 'make']\n",
    "pkg/base.py": "class Base:\n    def ping(self):\n        return 1\n    def hook(self):\n        return 0\n",
    "pkg/svc.py": '''
        from .base import Base
        def traced(fn):
            return fn
        def make():
            return Service()
        class Service(Base):
            def handle(self):
                return self.ping()
            def hook(self):
                return super().hook()
        ''',
    "pkg/util.py": "def open(path):\n    return path\ndef callback():\n    return 1\ndef inner():\n    return 2\n"
                   "def helper():\n    return 3\n",
    "app/__init__.py": "",
    "app/deep/__init__.py": "",
    "app/consumer.py": '''
        from pkg import Service, make
        from pkg.svc import traced
        from pkg.svc import Service as S2
        def instance_call():
            s = S2()
            s.handle()
        def annotated(svc: S2):
            svc.handle()
        def with_ctx():
            with S2() as c:
                c.handle()
        def reexport_call():
            Service.handle(None)
            make()
        @traced
        def decorated():
            return 1
        def builtin_shadow():
            return open("x")
        def param_shadow(callback):
            return callback()
        def nested():
            def inner():
                return 0
            return inner()
        ''',
    "app/star.py": "from pkg.util import *\ndef go():\n    return helper()\n",
    "app/deep/mod.py": "from ... import pkg\nfrom ..consumer import make\n",
    "app/member_only.py": "from pkg import svc as module\ndef go():\n    return module.Service.handle(None)\n",
}


class ProbeTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build(PROBE)

    def test_should_resolve_instance_annotated_with_and_reexported_calls(self):
        handle = "pkg/svc.py::Service.handle"
        for caller in ("instance_call", "annotated", "with_ctx", "reexport_call"):
            with self.subTest(caller=caller):
                self.assertIn(handle, self.targets(self.con, f"app/consumer.py::{caller}"))
        self.assertIn("pkg/svc.py::make", self.targets(self.con, "app/consumer.py::reexport_call"))
        calls = self.edges(self.con, "calls")
        self.assertEqual(calls[("app/consumer.py::instance_call", "calls", handle)], (1.0, "observed"),
                         "the class is imported, so the instance call is observed")

    def test_should_follow_inherited_and_super_calls_across_files(self):
        self.assertIn("pkg/base.py::Base.ping", self.targets(self.con, "pkg/svc.py::Service.handle"))
        self.assertIn("pkg/base.py::Base.hook", self.targets(self.con, "pkg/svc.py::Service.hook"))

    def test_inheritance_starts_at_the_class(self):
        inherits = self.edges(self.con, "inherits")
        self.assertIn(("pkg/svc.py::Service", "inherits", "pkg/base.py::Base"), inherits)
        self.assertFalse(any(src == "pkg/svc.py::" for src, _, _ in inherits))

    def test_should_never_link_builtins_parameters_or_nested_names_elsewhere(self):
        util = {f"pkg/util.py::{name}" for name in ("open", "callback", "inner")}
        for caller in ("builtin_shadow", "param_shadow", "nested"):
            with self.subTest(caller=caller):
                self.assertFalse(self.targets(self.con, f"app/consumer.py::{caller}") & util)
        self.assertEqual(self.targets(self.con, "app/consumer.py::nested"), {"app/consumer.py::nested.inner"},
                         "a nested definition is linked to itself, observed")

    def test_star_import_resolves_through_the_source_module(self):
        self.assertEqual(self.edges(self.con, "calls")[("app/star.py::go", "calls", "pkg/util.py::helper")],
                         (1.0, "observed"))

    def test_relative_imports_above_the_package_and_reexports_through_modules(self):
        imports = self.edges(self.con, "imports")
        self.assertIn(("file:app/deep/mod.py", "imports", "file:pkg/__init__.py"), imports)
        self.assertIn(("file:app/deep/mod.py", "imports", "file:app/consumer.py"), imports)
        self.assertIn(("file:app/deep/mod.py", "uses", "pkg/svc.py::make"), self.edges(self.con, "uses"),
                      "`make` re-exported by consumer.py and pkg/__init__.py is followed to its definition")

    def test_symbol_references_are_uses_edges(self):
        uses = self.edges(self.con, "uses")
        self.assertIn(("file:app/consumer.py", "uses", "pkg/svc.py::traced"), uses)
        self.assertIn(("app/consumer.py::decorated", "uses", "pkg/svc.py::traced"), uses)
        self.assertIn(("app/consumer.py::annotated", "uses", "pkg/svc.py::Service"), uses)
        rows, _ = code.impact(self.con, "pkg/svc.py::traced", depth=1)
        self.assertEqual([row["file"] for row in rows], ["app/consumer.py"], "impact of a decorator finds its users")

    def test_class_impact_includes_its_members(self):
        rows, _ = code.impact(self.con, "pkg/svc.py::Service", depth=1)
        self.assertIn("app/member_only.py", {row["file"] for row in rows},
                      "a caller of Service.handle depends on the class")

    def test_syntax_error_keeps_the_last_good_parse(self):
        svc = self.repo / "pkg/svc.py"
        svc.write_text(svc.read_text() + "\ndef broken(:\n")
        report = code.index(self.con, self.repo, max_seconds=0)
        self.assertEqual(report["stale"], ["pkg/svc.py"])
        self.assertIn("pkg/svc.py::Service.handle", {row["id"] for row in code.symbols_of(self.con, "pkg/svc.py")})
        self.assertEqual(code.find(self.con, "Service")[0]["id"], "pkg/svc.py::Service")
        rows, _ = code.impact(self.con, "pkg/svc.py", depth=1)
        self.assertIn("app/consumer.py", {row["file"] for row in rows})
        self.assertTrue(code.file_state(self.con, "pkg/svc.py")["stale_parse"])


class ScopeTests(CodeRepoCase):
    def test_should_respect_every_local_binding_form(self):
        names = ("item", "entry", "err", "found", "loop", "ctx", "walrus", "glob", "listed", "outer_local", "lam",
                 "deleted", "annotated", "imported_here")
        repo, con = self.build({
            "lib/__init__.py": "",
            "lib/names.py": "".join(f"def {name}():\n    return 1\n" for name in names),
            "use.py": '''
                import os
                from os.path import join as imported_here
                glob = None

                def comprehension(rows):
                    return [item() for item in rows] + [entry() for entry in {x: x for x in rows}]

                def handlers():
                    try:
                        pass
                    except ValueError as err:
                        err()

                def targets(rows, found):
                    for loop in rows:
                        loop()
                    with os.fdopen(3) as ctx:
                        ctx()
                    if (walrus := rows):
                        walrus()
                    found()

                def uses_global():
                    global glob
                    glob()

                def unpacked(rows):
                    listed, other = rows
                    listed()

                def enclosing():
                    outer_local = make()
                    def inner():
                        nonlocal outer_local
                        return outer_local()
                    return inner()

                def lambdas():
                    return (lambda lam: lam())(None)

                def deleting(deleted):
                    del deleted
                    deleted()

                def annotated_local():
                    annotated: int = 3
                    annotated()

                def imported():
                    imported_here()
                ''',
        })
        linked = {dst for (src, _, dst) in self.edges(con) if src.startswith("use.py")}
        self.assertFalse({f"lib/names.py::{name}" for name in names} & linked, sorted(linked))

    def test_should_not_link_rebound_or_untyped_instances(self):
        repo, con = self.build({
            "client.py": "class Client:\n    def send(self):\n        return 1\n    def close(self):\n        return 0\n",
            "use.py": '''
                from client import Client

                def rebound(flag):
                    c = Client()
                    if flag:
                        c = other()
                    c.send()

                def untyped(c):
                    c.send()

                def chained():
                    c = Client()
                    c.send().close()

                class Holder:
                    def __init__(self, mode):
                        self.client = Client() if mode else None
                        self.fixed = Client()

                    def run(self):
                        self.client.send()
                        self.fixed.close()
                ''',
        })
        for caller in ("rebound", "untyped"):
            self.assertNotIn("client.py::Client.send", self.targets(con, f"use.py::{caller}"))
        self.assertIn("client.py::Client.send", self.targets(con, "use.py::chained"))
        self.assertNotIn("client.py::Client.close", self.targets(con, "use.py::chained"), "a call result is not typed")
        self.assertEqual(self.targets(con, "use.py::Holder.run"), {"client.py::Client.close"},
                         "self.client is not one class, self.fixed is")

    def test_attribute_and_module_level_instances(self):
        repo, con = self.build({
            "pkg/__init__.py": "",
            "pkg/client.py": "class Client:\n    def send(self):\n        return 1\n",
            "pkg/base.py": '''
                from pkg.client import Client

                class Base:
                    def __init__(self):
                        self.client = Client()
                ''',
            "pkg/child.py": '''
                from pkg.base import Base
                from pkg.client import Client

                DEFAULT = Client()

                class Child(Base):
                    transport: Client

                    def go(self):
                        self.client.send()

                    def again(self):
                        self.transport.send()

                def module_level():
                    DEFAULT.send()

                def constructed():
                    Client().send()
                ''',
        })
        for caller in ("Child.go", "Child.again", "module_level", "constructed"):
            with self.subTest(caller=caller):
                self.assertIn("pkg/client.py::Client.send", self.targets(con, f"pkg/child.py::{caller}"))

    def test_inferred_class_makes_the_instance_call_inferred(self):
        repo, con = self.build({
            "lib/widget.py": "class Widget:\n    def draw(self):\n        return 1\n",
            "app.py": "def go():\n    w = Widget()\n    w.draw()\n",
        })
        self.assertEqual(self.edges(con, "calls")[("app.py::go", "calls", "lib/widget.py::Widget.draw")],
                         (0.85, "inferred"))

    def test_review_probe_p3_third_party_builtin_and_nested_names(self):
        repo, con = self.build({
            "pkg/__init__.py": "",
            "pkg/fileio.py": "def open(path):\n    return path\n\ndef loads(text):\n    return text\n",
            "pkg/client.py": ("from json import loads\n\ndef read(path):\n    with open(path) as f:\n"
                              "        return loads(f.read())\n\ndef run():\n    def helper():\n        return 1\n"
                              "    return helper()\n"),
            "pkg/tools.py": "def helper():\n    return 2\n",
        })
        self.assertEqual(code.impact(con, "pkg/fileio.py", 1, 10), ([], 0))
        self.assertEqual(self.targets(con, "pkg/client.py::run"), {"pkg/client.py::run.helper"})
        self.assertEqual(self.targets(con, "pkg/client.py::read"), set())


class StarAndClassMethodTests(CodeRepoCase):
    def test_star_imports_honour_all_underscores_and_refuse_ambiguity(self):
        repo, con = self.build({
            "pkg/__init__.py": "",
            "pkg/listed.py": "__all__ = ['shown']\ndef shown():\n    return 1\ndef hidden():\n    return 2\n",
            "pkg/public.py": "def _private():\n    return 1\ndef public():\n    return 2\ndef twice():\n    return 3\n",
            "pkg/again.py": "def twice():\n    return 4\n",
            "pkg/dynamic.py": "__all__ = ['made'] + list(range(0))\ndef made():\n    return 5\n",
            "use.py": '''
                from pkg.listed import *
                from pkg.public import *
                from pkg.again import *

                def go():
                    return shown() + hidden() + _private() + public() + twice()
                ''',
            "use_dynamic.py": "from pkg.dynamic import *\ndef go():\n    return made()\n",
        })
        self.assertEqual(self.targets(con, "use.py::go"), {"pkg/listed.py::shown", "pkg/public.py::public"})
        self.assertEqual(self.targets(con, "use_dynamic.py::go"), set(), "a computed __all__ is not guessed")

    def test_classmethods_and_cls_calls(self):
        repo, con = self.build({
            "model.py": '''
                class Model:
                    @classmethod
                    def build(cls):
                        return cls.make()

                    @classmethod
                    def make(cls):
                        return cls()

                    @staticmethod
                    def plain(self):
                        return self.make()
                ''',
        })
        self.assertEqual(self.targets(con, "model.py::Model.build"), {"model.py::Model.make"})
        self.assertEqual(self.targets(con, "model.py::Model.make"), {"model.py::Model"})
        self.assertEqual(self.targets(con, "model.py::Model.plain"), set(), "a staticmethod has no self")


class ImportRootTests(CodeRepoCase):
    def test_should_derive_roots_from_package_boundaries(self):
        repo, con = self.build({
            "backend/app/__init__.py": "",
            "backend/app/models.py": "class User:\n    def save(self):\n        return 1\n",
            "backend/app/api.py": "from app.models import User\ndef create():\n    User.save(None)\n",
            "packages/core/src/core/__init__.py": "",
            "packages/core/src/core/x.py": "def f():\n    return 1\n",
            "packages/web/src/web/__init__.py": "",
            "packages/web/src/web/y.py": "from core.x import f\ndef g():\n    return f()\n",
            "ns/tools.py": "def run():\n    return 1\n",
            "use_ns.py": "import ns.tools\ndef go():\n    return ns.tools.run()\n",
        })
        calls = self.edges(con, "calls")
        self.assertIn(("backend/app/api.py::create", "calls", "backend/app/models.py::User.save"), calls)
        self.assertIn(("packages/web/src/web/y.py::g", "calls", "packages/core/src/core/x.py::f"), calls)
        self.assertIn(("use_ns.py::go", "calls", "ns/tools.py::run"), calls)

    def test_should_refuse_an_ambiguous_module_name(self):
        repo, con = self.build({
            "one/utils.py": "def tool():\n    return 1\n",
            "two/utils.py": "def tool():\n    return 2\n",
            "three/main.py": "import utils\ndef go():\n    return utils.tool()\n",
            "one/near.py": "import utils\ndef go():\n    return utils.tool()\n",
        })
        self.assertEqual(self.targets(con, "three/main.py::go"), set())
        self.assertEqual(self.targets(con, "one/near.py::go"), {"one/utils.py::tool"}, "the script's own directory wins")


class TestHelperTests(CodeRepoCase):
    def test_should_prefer_test_helpers_for_tests(self):
        repo, con = self.build({
            "src/lib/h.py": "def make_fixture():\n    return 1\n\ndef shared():\n    return 1\n",
            "tests/helpers.py": "def make_fixture():\n    return 2\n",
            "tests/test_x.py": "from helpers import *\ndef test_a():\n    return make_fixture()\n",
            "tests/test_y.py": "def test_b():\n    return make_fixture()\n",
            "tests/unit/deep/test_z.py": "def test_c():\n    return near()\n",
            "tests/unit/deep/support.py": "def near():\n    return 1\n",
            "tests/other/support.py": "def near():\n    return 2\n",
            "src/app.py": "def run():\n    return make_fixture()\n",
        })
        calls = self.edges(con, "calls")
        self.assertEqual(calls[("tests/test_x.py::test_a", "calls", "tests/helpers.py::make_fixture")], (1.0, "observed"))
        self.assertEqual(calls[("tests/test_y.py::test_b", "calls", "tests/helpers.py::make_fixture")], (0.75, "inferred"))
        self.assertEqual(calls[("src/app.py::run", "calls", "src/lib/h.py::make_fixture")], (0.75, "inferred"))
        self.assertEqual(calls[("tests/unit/deep/test_z.py::test_c", "calls", "tests/unit/deep/support.py::near")],
                         (0.6, "inferred"))


class OverrideAndSiteTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build({
            "pkg/__init__.py": "",
            "pkg/base.py": "class Base:\n    def ping(self):\n        return 1\n",
            "pkg/sub.py": "from pkg.base import Base\n\nclass Sub(Base):\n    pass\n",
            "pkg/over.py": "from pkg.base import Base\n\nclass Over(Base):\n    def ping(self):\n        return 2\n",
            "pkg/user.py": "from pkg.sub import Sub\n\ndef use():\n    return Sub().ping()\n",
            "pkg/typed.py": "from pkg.base import Base\n\ndef run(item: Base):\n    return item.ping()\n",
            "pkg/many.py": '''
                from pkg.base import Base

                def thrice(item: Base):
                    item.ping()
                    item.ping()
                    return item.ping()
                ''',
            "pkg/workers.py": '''
                import threading
                from pkg.base import Base

                def work():
                    return 1

                def start(item: Base):
                    threading.Thread(target=work).start()
                    return list(map(item.ping, []))

                def checks(value):
                    try:
                        return isinstance(value, Base)
                    except (Base, ValueError):
                        return None
                ''',
        })

    def test_impact_of_a_base_method_includes_callers_through_subclasses(self):
        rows, _ = code.impact(self.con, "pkg/base.py::Base.ping", depth=1)
        self.assertIn("pkg/user.py", {row["file"] for row in rows}, "Sub().ping() runs Base.ping")

    def test_impact_of_an_override_includes_callers_of_the_base_method(self):
        rows, _ = code.impact(self.con, "pkg/over.py::Over.ping", depth=1)
        self.assertIn("pkg/typed.py", {row["file"] for row in rows}, "item.ping() typed Base may run Over.ping")

    def test_every_call_site_is_counted(self):
        sites, lines = self.con.execute("SELECT sites, lines FROM code_edge WHERE src='pkg/many.py::thrice' AND "
                                        "dst='pkg/base.py::Base.ping'").fetchone()
        self.assertEqual(sites, 3)
        self.assertEqual(lines, "4,5,6")

    def test_callables_passed_as_arguments_are_indirect_and_inferred(self):
        indirect = self.edges(self.con, "indirect")
        self.assertEqual(indirect[("pkg/workers.py::start", "indirect", "pkg/workers.py::work")], (0.6, "inferred"))
        self.assertEqual(indirect[("pkg/workers.py::start", "indirect", "pkg/base.py::Base.ping")], (0.6, "inferred"))

    def test_isinstance_and_except_classes_are_used(self):
        self.assertIn(("pkg/workers.py::checks", "uses", "pkg/base.py::Base"), self.edges(self.con, "uses"))


class TestPathTests(unittest.TestCase):
    def test_test_path_table(self):
        cases = {"tests/x.py": True, "test/x.py": True, "spec/x.rb": True, "specs/test_specs_dir.py": True,
                 "specs/x.py": True, "web/__tests__/a.js": True, "pkg/test_core.py": True, "pkg/core_test.go": True,
                 "src/FooTest.java": True, "src/FooTests.cs": True, "lib/user_spec.rb": True, "web/a.spec.ts": True,
                 "web/a.test.js": True, "conftest.py": True, "src/conftest.py": True, "src/contest.py": False,
                 "latest_test_data/x.py": False, "src/testing_utils.py": False, "src/attest.py": False,
                 "pkg/protest/x.py": False}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(code.is_test(path), expected)


if __name__ == "__main__":
    unittest.main()
