#!/usr/bin/env python3
"""Tests for host/devcontainer_host.py.

    python3 test/host/test_devcontainer_host.py

The integration test applies test/host/fixture for real and so needs root;
it confines itself to a temporary directory (state, cache, profile script and
everything the fixture writes) and is skipped otherwise.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FIXTURE = os.path.join(HERE, "fixture")


def load(env=None):
    """Imports a fresh copy, so module-level paths pick up `env`."""
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location(
            "devcontainer_host", os.path.join(ROOT, "host", "devcontainer_host.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


dh = load()


class JsoncTest(unittest.TestCase):
    def test_comments_and_trailing_commas(self):
        text = """
        // leading
        {
            "url": "https://example.com/a//b", /* inline */
            "glob": "src/*.ml /* not a comment */",
            "list": [1, 2,],
            "nested": {"a": "x,}",},
        }
        """
        self.assertEqual(dh.json.loads(dh.strip_jsonc(text)), {
            "url": "https://example.com/a//b",
            "glob": "src/*.ml /* not a comment */",
            "list": [1, 2],
            "nested": {"a": "x,}"},
        })

    def test_escaped_quote_in_string(self):
        self.assertEqual(dh.json.loads(dh.strip_jsonc(r'{"a": "say \"//hi\"",}')),
                         {"a": 'say "//hi"'})


class VariableTest(unittest.TestCase):
    ctx = {"localWorkspaceFolder": "/w/proj", "localWorkspaceFolderBasename": "proj"}

    def test_local_env_default_may_contain_colons(self):
        os.environ.pop("DCH_TEST_UNSET", None)
        self.assertEqual(dh.substitute("${localEnv:DCH_TEST_UNSET:debian:13}", self.ctx), "debian:13")
        os.environ["DCH_TEST_SET"] = "ubuntu:24.04"
        try:
            self.assertEqual(dh.substitute("${localEnv:DCH_TEST_SET:debian:13}", self.ctx), "ubuntu:24.04")
        finally:
            del os.environ["DCH_TEST_SET"]
        self.assertEqual(dh.substitute("${localEnv:DCH_TEST_UNSET}", self.ctx), "")

    def test_workspace_and_deferred_variables(self):
        value = {"a": ["${localWorkspaceFolderBasename}", "${containerEnv:X:dflt}", "${NVM_DIR}"]}
        self.assertEqual(dh.substitute(value, self.ctx),
                         {"a": ["proj", "${containerEnv:X:dflt}", "${NVM_DIR}"]})
        self.assertEqual(dh.substitute("${containerEnv:X:dflt}/${containerEnv:Y}", self.ctx, {"Y": "y"}),
                         "dflt/y")

    def test_shell_expansion(self):
        env = {"A": "a", "E": ""}
        cases = {
            "$A-${A}": "a-a",
            "${E:-fallback}": "fallback",
            "${A:-fallback}": "a",
            "${A:+set}${E:+set}": "set",
            r"\$A": "$A",
            "$UNSET.": ".",
            "cost $5": "cost $5",
        }
        for text, want in cases.items():
            self.assertEqual(dh.expand_shell_vars(text, env), want, text)

    def test_option_env_names(self):
        cases = {"base-packages": "BASE_PACKAGES", "installZsh": "INSTALLZSH",
                 "version": "VERSION", "1st.option": "_ST_OPTION", "_x": "_X"}
        for option, want in cases.items():
            self.assertEqual(dh.option_env_name(option), want, option)


class DockerfileTest(unittest.TestCase):
    def test_continuations_comments_and_heredocs(self):
        text = (
            "# syntax=docker/dockerfile:1\n"
            "ARG V=1\n"
            "FROM debian:${V}\n"
            "RUN set -eux; \\\n"
            "# a comment inside the continuation\n"
            "\n"
            "    echo one; \\\n"
            "    echo two\n"
            "RUN <<EOF\n"
            "echo heredoc\n"
            "EOF\n"
            "COPY <<-CONF /etc/x.conf\n"
            "\tkey=value\n"
            "CONF\n"
            "env A=b\n"
        )
        ins = dh.parse_dockerfile(text)
        self.assertEqual([i.keyword for i in ins], ["ARG", "FROM", "RUN", "RUN", "COPY", "ENV"])
        self.assertEqual(ins[2].args, "set -eux;     echo one;     echo two")
        self.assertEqual(ins[3].heredocs, [("EOF", "echo heredoc\n")])
        self.assertEqual(ins[4].heredocs, [("CONF", "key=value\n")])

    def test_escape_directive(self):
        ins = dh.parse_dockerfile("# escape=`\nFROM x\nRUN echo a `\n  b\n")
        self.assertEqual(ins[1].args, "echo a \n  b")

    def test_stages_args_and_target(self):
        text = ("ARG BASE=debian\nARG TAG\nFROM ${BASE}:${TAG} AS builder\nRUN a\n"
                "FROM builder AS dev\nRUN b\nFROM scratch AS release\nRUN c\n")
        global_args, stages = dh.split_stages(dh.parse_dockerfile(text), {"TAG": "13"})
        self.assertEqual(global_args, {"BASE": "debian", "TAG": "13"})
        self.assertEqual(stages[0].base, "debian:13")
        self.assertEqual([s.name for s in dh.stage_chain(stages, "dev")], ["builder", "dev"])
        self.assertEqual([s.name for s in dh.stage_chain(stages, None)], ["release"])
        with self.assertRaises(dh.Error):
            dh.stage_chain(stages, "missing")

    def test_env_pairs(self):
        env = {"PATH": "/usr/bin"}
        self.assertEqual(dh.parse_env_pairs('A=1 B="two words" P=/x:${PATH}', env),
                         [("A", "1"), ("B", "two words"), ("P", "/x:/usr/bin")])
        self.assertEqual(dh.parse_env_pairs("LEGACY value with spaces", env),
                         [("LEGACY", "value with spaces")])
        self.assertEqual(dh._raw_env_value("P=/x:${PATH}", "P"), "/x:${PATH}")


class FeatureTest(unittest.TestCase):
    def test_references(self):
        r = dh.FeatureRef("ghcr.io/devcontainers/features/node:2", "/cfg")
        self.assertEqual((r.kind, r.registry, r.repo, r.reference, r.id),
                         ("oci", "ghcr.io", "devcontainers/features/node", "2", "node"))
        digest = "sha256:" + "a" * 64
        r = dh.FeatureRef(f"ghcr.io/o/r/f@{digest}", "/cfg")
        self.assertEqual((r.reference, r.canonical), (digest, "ghcr.io/o/r/f"))
        r = dh.FeatureRef("ghcr.io/o/r/f", "/cfg")
        self.assertEqual(r.reference, "latest")
        r = dh.FeatureRef("./features/ocaml", "/w/.devcontainer")
        self.assertEqual((r.kind, r.path, r.id), ("local", "/w/.devcontainer/features/ocaml", "ocaml"))
        r = dh.FeatureRef("https://example.com/devcontainer-feature-go.tgz", "/cfg")
        self.assertEqual((r.kind, r.id), ("tarball", "go"))

    def _features(self, specs):
        out = []
        for i, (ref, meta) in enumerate(specs):
            f = dh.Feature(dh.FeatureRef(ref, "/cfg"), {}, i)
            f.meta, f.dir = meta, "/cfg"
            out.append(f)
        return out

    def test_order_installs_after_and_depends_on(self):
        fs = self._features([
            ("ghcr.io/x/f/a:1", {"installsAfter": ["ghcr.io/x/f/c"]}),
            ("ghcr.io/x/f/b:1", {}),
            ("ghcr.io/x/f/c:1", {"dependsOn": {"ghcr.io/x/f/b:1": {}}}),
            ("ghcr.io/x/f/d:1", {"installsAfter": ["ghcr.io/x/f/not-present"]}),
        ])
        order = [f.fref.id for f in dh.order_features(fs, [], "/cfg")]
        self.assertEqual(order, ["b", "c", "a", "d"])

    def test_override_order(self):
        fs = self._features([("ghcr.io/x/f/a:1", {}), ("ghcr.io/x/f/b:1", {})])
        order = [f.fref.id for f in dh.order_features(fs, ["ghcr.io/x/f/b"], "/cfg")]
        self.assertEqual(order, ["b", "a"])

    def test_cycle(self):
        fs = self._features([
            ("ghcr.io/x/f/a:1", {"installsAfter": ["ghcr.io/x/f/b"]}),
            ("ghcr.io/x/f/b:1", {"installsAfter": ["ghcr.io/x/f/a"]}),
        ])
        with self.assertRaises(dh.Error):
            dh.order_features(fs, [], "/cfg")

    def test_option_environment(self):
        f = dh.Feature(dh.FeatureRef("./f", "/cfg"), {"loud": True, "message": "${localWorkspaceFolderBasename}"}, 0)
        f.meta = {"options": {"loud": {"type": "boolean", "default": False},
                              "message": {"type": "string", "default": "hi"},
                              "extra-words": {"type": "string", "default": "w"}}}
        env = dh.feature_env(f, {"localWorkspaceFolderBasename": "proj"})
        self.assertEqual((env["LOUD"], env["MESSAGE"], env["EXTRA_WORDS"], env["_REMOTE_USER"]),
                         ("true", "proj", "w", "root"))


@unittest.skipUnless(os.geteuid() == 0 and shutil.which("bash"), "needs root and bash")
class FixtureTest(unittest.TestCase):
    """Applies test/host/fixture for real, inside a temporary directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dch-test-")
        self.root = os.path.join(self.tmp, "out")
        os.makedirs(self.root)
        self.workspace = os.path.join(self.tmp, "proj")
        shutil.copytree(FIXTURE, self.workspace)
        self.env = {
            "DEVCONTAINER_HOST_STATE": os.path.join(self.tmp, "state"),
            "DEVCONTAINER_HOST_CACHE": os.path.join(self.tmp, "cache"),
            "DEVCONTAINER_HOST_PROFILE": os.path.join(self.tmp, "profile.sh"),
            "DEVCONTAINER_HOST_BASHRC": os.path.join(self.tmp, "bashrc"),
        }
        open(self.env["DEVCONTAINER_HOST_BASHRC"], "w").close()
        os.environ["FIXTURE_ROOT"] = self.root
        self.dh = load(self.env)

    def tearDown(self):
        os.environ.pop("FIXTURE_ROOT", None)
        shutil.rmtree(self.tmp)

    def run_tool(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.dh.main(["--workspace-folder", self.workspace,
                                 "--env-file", os.path.join(self.tmp, "env.sh"), *extra])
        return code, out.getvalue(), err.getvalue()

    def read(self, *path):
        with open(os.path.join(self.root, *path)) as f:
            return f.read()

    def test_apply_and_rerun(self):
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, out + err)

        # Dockerfile: build.args (with ${localEnv:...} default), ARG default,
        # WORKDIR, guarded userdel, COPY of a directory's contents, --chmod,
        # heredoc script, SHELL, USER downgraded to root.
        self.assertEqual(self.read("args"), "args: yes \n")
        self.assertEqual(self.read("copied", "data.txt"), "copied file\n")
        self.assertEqual(self.read("tool.out"), "tool ran\n")
        self.assertEqual(self.read("heredoc"), "heredoc\n")
        self.assertEqual(self.read("user"), "user: root\n")
        self.assertEqual(self.read("guarded"), "guarded\n")
        self.assertIn("USER vscode: running as root", err)

        # Feature: options with defaults, boolean, substitution; ENV visible.
        self.assertEqual(self.read("feature"),
                         "MESSAGE=hello from proj\nLOUD=true\nEXTRA_WORDS=default words\n"
                         "_REMOTE_USER=root\nFIXTURE_ENV=dockerfile\n")
        self.assertIn("skipped: ghcr.io/devcontainers/features/common-utils:2", out)

        # Lifecycle: order, feature hooks before the config's, sudo, env.
        self.assertEqual(self.read("lifecycle").split(),
                         ["initialize", "onCreate", "feature-postCreate", "postCreate", "postStart"])
        self.assertEqual(self.read("env"), "remote-container greeting-feature dockerfile\n")

        # Persisted environment keeps ${PATH} relative for login shells.
        with open(self.env["DEVCONTAINER_HOST_PROFILE"]) as f:
            profile = f.read()
        self.assertIn('export FIXTURE_PATH="${GREETING_DIR}/bin:${PATH}"', profile)
        self.assertIn('export PATH="/opt/greeting/bin:${PATH}"', profile)
        self.assertIn('export FIXTURE_REMOTE_ENV="remote-container"', profile)
        with open(self.env["DEVCONTAINER_HOST_BASHRC"]) as f:
            self.assertEqual(f.read().count("# devcontainer-host"), 1)
        with open(os.path.join(self.tmp, "env.sh")) as f:
            self.assertIn('export GREETING="greeting-feature"', f.read())

        # Rerun: every Dockerfile step and the feature are cached, the
        # lifecycle commands run again, the bashrc hook is not duplicated.
        os.remove(os.path.join(self.root, "feature"))
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("cached"), 7, out)  # 6 Dockerfile steps + feature
        self.assertFalse(os.path.exists(os.path.join(self.root, "feature")))
        self.assertEqual(self.read("lifecycle").split()[0], "initialize")
        with open(self.env["DEVCONTAINER_HOST_BASHRC"]) as f:
            self.assertEqual(f.read().count("# devcontainer-host"), 1)

        # Editing the local feature invalidates only its stamp.
        with open(os.path.join(self.workspace, ".devcontainer/features/greeting/install.sh"), "a") as f:
            f.write("echo edited >> \"$FIXTURE_ROOT/feature\"\n")
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("cached"), 6, out)
        self.assertIn("edited", self.read("feature"))

    def test_dry_run_changes_nothing(self):
        code, out, err = self.run_tool("--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(os.path.exists(self.env["DEVCONTAINER_HOST_PROFILE"]))
        self.assertIn("$ greeting/install.sh", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
