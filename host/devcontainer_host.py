#!/usr/bin/env python3
"""Apply a devcontainer.json to the current machine, without Docker.

Provisions a host (typically a throwaway cloud VM or container, such as a
Claude Code cloud environment) the way `devcontainer build` + `devcontainer
up` would provision a container, by replaying the configuration directly:

  1. the Dockerfile's target stage: ARG, ENV, WORKDIR, SHELL, RUN, COPY and
     ADD, in order, with build.args applied (FROM is not pulled);
  2. the features, fetched from their OCI registry (or a local directory or
     tarball), ordered by dependsOn/installsAfter, with their options
     exported as environment variables, the same way the CLI runs install.sh;
  3. the lifecycle commands (initialize, onCreate, updateContent, postCreate,
     postStart, postAttach), the features' before the config's own.

Everything runs as root on the host. Settings that only make sense for a
container (runArgs, mounts, ports, remoteUser, ...) are reported and ignored.

Reruns are cheap: each Dockerfile step and feature is stamped, and a stamped
step is skipped until it (or, for Dockerfile steps, anything before it)
changes, mirroring Docker's layer cache.

Usage:
  devcontainer_host.py [--workspace-folder DIR] [--config FILE] [options]
  curl -fsSL .../devcontainer_host.py | python3 - --workspace-folder DIR

Only the Python 3 standard library is required (plus git for the GitHub
feature fallback, and whatever the Dockerfile and features themselves need).
"""

import argparse
import fnmatch
import glob
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

PROG = "devcontainer-host"
STATE_DIR = os.environ.get("DEVCONTAINER_HOST_STATE", "/var/lib/devcontainer-host")
# Root-owned by default; a non-root --dry-run falls back to the user's cache.
CACHE_DIR = os.environ.get("DEVCONTAINER_HOST_CACHE") or (
    "/var/cache/devcontainer-host" if os.geteuid() == 0
    else os.path.join(os.path.expanduser("~/.cache"), "devcontainer-host"))
PROFILE_SCRIPT = os.environ.get("DEVCONTAINER_HOST_PROFILE", "/etc/profile.d/devcontainer-host.sh")
BASHRC = os.environ.get("DEVCONTAINER_HOST_BASHRC", "/etc/bash.bashrc")

# Features that need a container runtime or that manage the container user;
# none of them are meaningful on a host where everything runs as root.
DEFAULT_SKIP = ("common-utils", "docker-outside-of-docker", "docker-in-docker")

# Settings that only apply to a container; reported, then ignored.
CONTAINER_ONLY = (
    "appPort", "capAdd", "containerUser", "forwardPorts", "hostRequirements",
    "init", "mounts", "otherPortsAttributes", "overrideCommand",
    "portsAttributes", "privileged", "remoteUser", "runArgs", "securityOpt",
    "shutdownAction", "updateRemoteUserUID", "workspaceFolder", "workspaceMount",
)
PROBE_FLAGS = {
    "loginInteractiveShell": ["-l", "-i"],
    "interactiveShell": ["-i"],
    "loginShell": ["-l"],
}
FEATURE_CONTAINER_ONLY = ("capAdd", "entrypoint", "init", "mounts", "privileged", "securityOpt")

LIFECYCLE = (
    "onCreateCommand", "updateContentCommand", "postCreateCommand",
    "postStartCommand", "postAttachCommand",
)

# Prepended to every shell script this tool runs. Dockerfiles written for a
# fresh image routinely delete the base image's default user (`userdel -r
# ubuntu`), which on a host would delete a real account.
HOST_GUARD = """\
userdel() { echo "devcontainer-host: skipped on host: userdel $*" >&2; }
deluser() { echo "devcontainer-host: skipped on host: deluser $*" >&2; }
groupdel() { echo "devcontainer-host: skipped on host: groupdel $*" >&2; }
delgroup() { echo "devcontainer-host: skipped on host: delgroup $*" >&2; }
"""
# Lifecycle commands are written for remoteUser and often escalate with sudo;
# as root that is a no-op, but a minimal host may not have sudo installed.
SUDO_SHIM = """\
sudo() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -u|-g|-C|-D|-h|-p|-r|-t|-U) shift 2 ;;
            --) shift; break ;;
            -*) shift ;;
            *) break ;;
        esac
    done
    "$@"
}
"""


class Error(Exception):
    pass


# --- logging -----------------------------------------------------------------

def log(msg):
    print(f"==> {msg}", flush=True)


def info(msg):
    print(f"    {msg}", flush=True)


def warn(msg):
    print(f"{PROG}: warning: {msg}", file=sys.stderr, flush=True)


# --- JSONC ---------------------------------------------------------------------

def strip_jsonc(text):
    """Removes // and /* */ comments and trailing commas, outside strings."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise Error("unterminated /* comment")
            i = end + 2
        else:
            out.append(c)
            i += 1
    stripped = "".join(out)
    # Trailing commas: only reachable outside strings once comments are gone
    # if we skip over strings again.
    out = []
    i, n = 0, len(stripped)
    while i < n:
        c = stripped[i]
        if c == '"':
            j = i + 1
            while j < n and stripped[j] != '"':
                j += 2 if stripped[j] == "\\" else 1
            out.append(stripped[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            j = i + 1
            while j < n and stripped[j] in " \t\r\n":
                j += 1
            if j < n and stripped[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(strip_jsonc(text))
    except (ValueError, Error) as e:
        raise Error(f"{path}: {e}") from e


# --- variables -------------------------------------------------------------------

VAR_RE = re.compile(r"\$\{([A-Za-z]+)(?::([^}:]+))?(?::([^}]*))?\}")


def substitute(value, ctx, container_env=None):
    """Expands devcontainer.json ${...} variables in strings, recursively.

    ${containerEnv:...} is only expanded when container_env is given (i.e. at
    the time a command runs); unknown variables such as ${NVM_DIR} are left
    for the shell, as the CLI does.
    """
    if isinstance(value, dict):
        return {k: substitute(v, ctx, container_env) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, ctx, container_env) for v in value]
    if not isinstance(value, str):
        return value

    def repl(m):
        kind, name, default = m.group(1), m.group(2), m.group(3)
        if kind in ("localEnv", "env") and name:
            return os.environ.get(name, default or "")
        if kind == "containerEnv" and name:
            if container_env is None:
                return m.group(0)
            return container_env.get(name, default or "")
        if name is None and kind in ctx:
            return ctx[kind]
        return m.group(0)

    return VAR_RE.sub(repl, value)


def expand_shell_vars(text, env):
    """Dockerfile-style expansion: $V, ${V}, ${V:-word}, ${V:+word}, \\$."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] == "$":
            out.append("$")
            i += 2
        elif c == "$" and i + 1 < n and text[i + 1] == "{":
            end = text.find("}", i + 2)
            if end < 0:
                out.append(text[i:])
                break
            body = text[i + 2:end]
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?::([-+])(.*))?$", body, re.S)
            if not m:
                out.append(text[i:end + 1])
            else:
                name, op, word = m.groups()
                val = env.get(name)
                if op == "-":
                    out.append(val if val else expand_shell_vars(word, env))
                elif op == "+":
                    out.append(expand_shell_vars(word, env) if val else "")
                else:
                    out.append(val or "")
            i = end + 1
        elif c == "$":
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i + 1:])
            if m:
                out.append(env.get(m.group(0), ""))
                i += 1 + m.end()
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def option_env_name(option_id):
    """Option id -> environment variable name, per the features spec."""
    name = re.sub(r"[^\w_]", "_", option_id)
    name = re.sub(r"^[\d_]+", "_", name)
    return name.upper()


# --- Dockerfile ------------------------------------------------------------------

class Instruction:
    def __init__(self, keyword, args, lineno, heredocs=()):
        self.keyword = keyword
        self.args = args
        self.lineno = lineno
        self.heredocs = list(heredocs)

    def text(self):
        parts = [f"{self.keyword} {self.args}"]
        for delim, body in self.heredocs:
            parts.append(body + delim)
        return "\n".join(parts)

    def __repr__(self):
        return f"Instruction({self.keyword!r}, {self.args!r}, line {self.lineno})"


HEREDOC_RE = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")


def parse_dockerfile(text):
    """Splits a Dockerfile into instructions, honouring the escape directive,
    comments, line continuations and heredocs."""
    lines = text.splitlines()
    escape = "\\"
    # Parser directives: only at the very top, before any other line.
    for line in lines:
        m = re.match(r"^\s*#\s*([a-zA-Z]+)\s*=\s*(\S+)\s*$", line)
        if not m:
            break
        if m.group(1).lower() == "escape":
            escape = m.group(2)
    instructions = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        start = i + 1
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        buf = line
        while buf.rstrip().endswith(escape):
            buf = buf.rstrip()[:-len(escape)]
            while i < n and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
                i += 1
            if i >= n:
                break
            buf += "\n" + lines[i] if escape == "`" else lines[i]
            i += 1
        buf = buf.strip()
        keyword, _, args = buf.partition(" ")
        if "\t" in keyword:
            keyword, _, rest = keyword.partition("\t")
            args = rest + " " + args
        keyword = keyword.upper()
        args = args.strip()
        heredocs = []
        if keyword in ("RUN", "COPY", "ADD"):
            for m in HEREDOC_RE.finditer(args):
                strip_tabs, delim = m.group(1) == "-", m.group(3)
                body = []
                while i < n and lines[i].rstrip("\r") != delim:
                    body.append(lines[i].lstrip("\t") if strip_tabs else lines[i])
                    i += 1
                i += 1
                heredocs.append((delim, "\n".join(body) + "\n"))
        instructions.append(Instruction(keyword, args, start, heredocs))
    return instructions


def split_flags(args):
    """Splits leading --flag[=value] options off an instruction's arguments."""
    flags = {}
    rest = args
    while rest.startswith("--"):
        token, _, rest = rest.partition(" ")
        key, _, value = token[2:].partition("=")
        flags.setdefault(key, []).append(value)
        rest = rest.lstrip()
    return flags, rest


def parse_json_array(args):
    if args.startswith("["):
        try:
            value = json.loads(args)
        except ValueError:
            return None
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return value
    return None


def parse_env_pairs(args, env):
    """ENV/ARG/LABEL key=value pairs (or the legacy `ENV key value` form)."""
    try:
        tokens = shlex.split(args, posix=True)
    except ValueError as e:
        raise Error(f"cannot parse {args!r}: {e}") from e
    if tokens and "=" not in tokens[0]:
        key, _, value = args.partition(" ")
        return [(key, expand_shell_vars(value.strip(), env))]
    pairs = []
    # Re-split without removing quotes from the raw text, so values expand
    # like Docker does (quoted $ still expands in double quotes).
    for token in tokens:
        key, _, value = token.partition("=")
        pairs.append((key, expand_shell_vars(value, env)))
    return pairs


class Stage:
    def __init__(self, base, name, index):
        self.base = base
        self.name = name
        self.index = index
        self.instructions = []


def split_stages(instructions, build_args):
    """Returns (global ARG values, stages)."""
    global_args = {}
    stages = []
    for ins in instructions:
        if ins.keyword == "FROM":
            flags, rest = split_flags(ins.args)
            parts = rest.split()
            base = expand_shell_vars(parts[0], global_args) if parts else ""
            name = parts[2] if len(parts) >= 3 and parts[1].upper() == "AS" else None
            stages.append(Stage(base, name, len(stages)))
        elif not stages:
            if ins.keyword == "ARG":
                for key, value in _arg_decls(ins.args, global_args):
                    if key in build_args:
                        global_args[key] = build_args[key]
                    elif value is not None:
                        global_args[key] = value
            else:
                raise Error(f"line {ins.lineno}: {ins.keyword} before FROM")
        else:
            stages[-1].instructions.append(ins)
    if not stages:
        raise Error("Dockerfile has no FROM")
    return global_args, stages


def _arg_decls(args, env):
    decls = []
    for token in shlex.split(args):
        key, eq, value = token.partition("=")
        decls.append((key, expand_shell_vars(value, env) if eq else None))
    return decls


def stage_chain(stages, target):
    """The stages to replay for `target`, base first."""
    by_name = {s.name: s for s in stages if s.name}
    if target:
        if target not in by_name:
            raise Error(f"build target {target!r} not found in Dockerfile")
        stage = by_name[target]
    else:
        stage = stages[-1]
    chain = [stage]
    while stage.base in by_name and by_name[stage.base].index < stage.index:
        stage = by_name[stage.base]
        chain.insert(0, stage)
    return chain


# --- host state --------------------------------------------------------------------

class Env:
    """Environment variables set by ENV / containerEnv / remoteEnv.

    Kept both as resolved values (for the processes this tool starts) and as
    an ordered list of assignments (for the profile script, so that
    `PATH=/x:${PATH}` stays relative to the login shell's PATH).
    """

    def __init__(self):
        self.values = {}
        self.assignments = []

    def environ(self, extra=None, under=None):
        """os.environ plus these variables; `under` is overridden by them
        (as ARG is by ENV), `extra` overrides them."""
        env = dict(os.environ)
        env.update(under or {})
        env.update(self.values)
        env.update(extra or {})
        return env

    def set(self, key, raw, resolved):
        self.values[key] = resolved
        self.assignments.append((key, raw))

    def write_profile(self, path=PROFILE_SCRIPT):
        lines = [
            "# Generated by devcontainer-host: environment from the devcontainer",
            "# configuration (Dockerfile ENV, containerEnv, remoteEnv).",
        ]
        for key, raw in self.assignments:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                warn(f"not exporting invalid variable name {key!r}")
                continue
            lines.append(f'export {key}="{_dq_escape(raw)}"')
        content = "\n".join(lines) + "\n"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        # Non-login interactive shells read bash.bashrc, not profile.d.
        hook = f"[ -r {path} ] && . {path}  # devcontainer-host"
        if os.path.exists(BASHRC):
            with open(BASHRC) as f:
                if hook in f.read():
                    return
            with open(BASHRC, "a") as f:
                f.write("\n" + hook + "\n")


def _dq_escape(value):
    """Escapes for a double-quoted shell string, keeping $VAR expansion."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")


class Stamps:
    def __init__(self, scope, force=False, dry_run=False):
        self.dir = os.path.join(STATE_DIR, scope)
        self.force = force
        self.dry_run = dry_run

    def done(self, key):
        return not self.force and os.path.exists(os.path.join(self.dir, key))

    def mark(self, key, label):
        if self.dry_run:
            return
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, key), "w") as f:
            f.write(label + "\n")


def sha256(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode() if isinstance(p, str) else p)
        h.update(b"\0")
    return h.hexdigest()


def tree_digest(path):
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(root, name)
            h.update(os.path.relpath(p, path).encode() + b"\0")
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            else:
                with open(p, "rb") as f:
                    h.update(f.read())
            h.update(b"\0")
    return h.hexdigest()


# --- running -------------------------------------------------------------------------

class Runner:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.have_sudo = shutil.which("sudo") is not None

    def shell(self, script, env, cwd, shell=("/bin/sh", "-c"), guard=True):
        prefix = ""
        if guard and os.path.basename(shell[0]) in ("sh", "bash", "dash", "ash", "zsh"):
            prefix = HOST_GUARD + ("" if self.have_sudo else SUDO_SHIM)
        self.argv(list(shell) + [prefix + script], env, cwd, display=script)

    def argv(self, argv, env, cwd, display=None):
        shown = display if display is not None else " ".join(shlex.quote(a) for a in argv)
        info(f"$ {shown}" if "\n" not in shown else "$ " + shown.replace("\n", "\n      "))
        if self.dry_run:
            return
        r = subprocess.run(argv, env=env, cwd=cwd)
        if r.returncode != 0:
            raise Error(f"command failed with exit code {r.returncode}: {shown.splitlines()[0]}")


# --- Dockerfile replay ------------------------------------------------------------------

def host_os():
    fields = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                fields[k] = v.strip('"')
    except OSError:
        pass
    return fields


def report_base_image(image):
    osr = host_os()
    host = osr.get("PRETTY_NAME", "unknown")
    info(f"base image {image} is not pulled; building on the host ({host})")
    ref = image.split("@")[0].rsplit("/", 1)[-1]
    distro, _, tag = ref.partition(":")
    host_id = osr.get("ID", "")
    if distro in ("debian", "ubuntu", "alpine", "fedora") and host_id and distro != host_id:
        warn(f"{image} is {distro}, but the host is {host_id}: package names and versions may differ")
    elif distro == "base" and "devcontainers" in image:
        warn(f"{image} ships a vscode user and common tools that are not recreated on the host")


def replay_dockerfile(cfg, cfg_dir, ctx, env, runner, stamps):
    build = cfg.get("build") or {}
    dockerfile = build.get("dockerfile") or cfg.get("dockerFile")
    if not dockerfile:
        if cfg.get("image"):
            log("image")
            report_base_image(cfg["image"])
        return
    path = os.path.normpath(os.path.join(cfg_dir, dockerfile))
    context = os.path.normpath(os.path.join(cfg_dir, build.get("context", ".")))
    build_args = {k: str(v) for k, v in (build.get("args") or {}).items()}
    log(f"Dockerfile {os.path.relpath(path, ctx['localWorkspaceFolder'])}")
    with open(path, encoding="utf-8") as f:
        instructions = parse_dockerfile(f.read())
    global_args, stages = split_stages(instructions, build_args)
    chain = stage_chain(stages, build.get("target"))
    report_base_image(chain[0].base)
    for opt in ("cacheFrom", "options"):
        if build.get(opt):
            info(f"build.{opt} ignored")

    key = sha256("dockerfile", path)
    workdir = "/"
    shell = ("/bin/sh", "-c")
    user_warned = False
    for stage in chain:
        args = {}
        for ins in stage.instructions:
            kw = ins.keyword
            scope = dict(global_args)
            scope.update(args)
            scope.update(env.values)
            if kw == "ARG":
                for k, v in _arg_decls(ins.args, scope):
                    if k in build_args:
                        args[k] = build_args[k]
                    elif v is not None:
                        args[k] = v
                    elif k in global_args:
                        args[k] = global_args[k]
                continue
            if kw == "ENV":
                for k, v in parse_env_pairs(ins.args, scope):
                    raw = _raw_env_value(ins.args, k)
                    env.set(k, raw if raw is not None else v, v)
                continue
            if kw == "WORKDIR":
                workdir = os.path.join(workdir, expand_shell_vars(ins.args, scope))
                continue
            if kw == "SHELL":
                value = parse_json_array(ins.args)
                if not value:
                    raise Error(f"line {ins.lineno}: SHELL needs a JSON array")
                shell = tuple(value)
                continue
            if kw == "USER":
                user = expand_shell_vars(ins.args, scope)
                if user.split(":")[0] not in ("root", "0") and not user_warned:
                    warn(f"line {ins.lineno}: USER {user}: running as root instead")
                    user_warned = True
                continue
            if kw in ("LABEL", "EXPOSE", "CMD", "ENTRYPOINT", "HEALTHCHECK",
                      "STOPSIGNAL", "VOLUME", "ONBUILD", "MAINTAINER"):
                continue
            if kw not in ("RUN", "COPY", "ADD"):
                raise Error(f"line {ins.lineno}: unsupported instruction {kw}")

            # Steps with effects: stamped like layers, keyed on everything
            # before them.
            step_env = {k: v for k, v in args.items()}
            key = sha256(key, ins.text(), json.dumps(step_env, sort_keys=True),
                         json.dumps(env.values, sort_keys=True), workdir, " ".join(shell))
            if kw in ("COPY", "ADD"):
                key = sha256(key, _copy_sources_digest(ins, context, scope))
            first = ins.text().splitlines()[0]
            if stamps.done(key):
                info(f"cached: {first[:100]}")
                continue
            if not runner.dry_run:
                os.makedirs(workdir, exist_ok=True)
            if kw == "RUN":
                _run_instruction(ins, shell, workdir, env.environ(under=step_env), runner)
            else:
                _copy_instruction(ins, context, workdir, scope, runner)
            stamps.mark(key, first)


def _raw_env_value(args, key):
    """The unexpanded value of `key` in an ENV instruction, for the profile."""
    try:
        for token in shlex.split(args):
            k, eq, v = token.partition("=")
            if eq and k == key:
                return v
    except ValueError:
        pass
    return None


def _run_instruction(ins, shell, workdir, environ, runner):
    flags, rest = split_flags(ins.args)
    for mount in flags.get("mount", []):
        if "type=secret" in mount or "type=ssh" in mount:
            warn(f"line {ins.lineno}: RUN --mount={mount} is not available on the host")
    argv = parse_json_array(rest)
    if argv is not None:
        runner.argv(argv, environ, workdir)
        return
    script = rest
    if ins.heredocs:
        bare = HEREDOC_RE.fullmatch(rest.strip())
        if bare and len(ins.heredocs) == 1:
            # `RUN <<EOF` runs the heredoc itself as the script.
            script = ins.heredocs[0][1]
        else:
            script = rest + "\n" + "".join(body + delim + "\n" for delim, body in ins.heredocs)
    runner.shell(script, environ, workdir, shell=shell)


def _copy_parts(ins, scope):
    flags, rest = split_flags(ins.args)
    parts = parse_json_array(rest)
    if parts is None:
        parts = shlex.split(rest)
    parts = [expand_shell_vars(p, scope) for p in parts]
    if len(parts) < 2:
        raise Error(f"line {ins.lineno}: {ins.keyword} needs a source and a destination")
    return flags, parts[:-1], parts[-1]


def _copy_sources_digest(ins, context, scope):
    flags, sources, _ = _copy_parts(ins, scope)
    h = []
    for src in sources:
        if re.match(r"^https?://", src) or HEREDOC_RE.fullmatch(src):
            h.append(src)
            continue
        for p in sorted(glob.glob(os.path.join(context, src))):
            h.append(tree_digest(p))
    return sha256(*h)


def _copy_instruction(ins, context, workdir, scope, runner):
    flags, sources, dest = _copy_parts(ins, scope)
    if flags.get("from"):
        raise Error(f"line {ins.lineno}: {ins.keyword} --from is not supported on the host")
    if flags.get("chown"):
        info(f"line {ins.lineno}: --chown ignored (files stay owned by root)")
    mode = int(flags["chmod"][0], 8) if flags.get("chmod") else None
    dest = os.path.join(workdir, dest)
    to_dir = dest.endswith("/") or len(sources) > 1
    info(f"{ins.keyword} {' '.join(sources)} -> {dest}")
    if runner.dry_run:
        return
    heredoc_bodies = dict(ins.heredocs)
    for src in sources:
        hd = HEREDOC_RE.fullmatch(src)
        if hd:
            target = dest if not to_dir else os.path.join(dest, hd.group(3))
            os.makedirs(os.path.dirname(target) or "/", exist_ok=True)
            with open(target, "w") as f:
                f.write(heredoc_bodies.get(hd.group(3), ""))
            _chmod(target, mode)
            continue
        if re.match(r"^https?://", src):
            if ins.keyword != "ADD":
                raise Error(f"line {ins.lineno}: COPY cannot fetch URLs")
            name = os.path.basename(urllib.parse.urlparse(src).path) or "download"
            target = os.path.join(dest, name) if to_dir else dest
            os.makedirs(os.path.dirname(target) or "/", exist_ok=True)
            with http_get(src) as r, open(target, "wb") as f:
                shutil.copyfileobj(r, f)
            _chmod(target, mode)
            continue
        matches = sorted(glob.glob(os.path.join(context, src)))
        if not matches:
            raise Error(f"line {ins.lineno}: {src}: no such file in build context {context}")
        for m in matches:
            if ins.keyword == "ADD" and os.path.isfile(m) and _is_tar(m):
                os.makedirs(dest, exist_ok=True)
                with tarfile.open(m) as t:
                    _safe_extract(t, dest)
            elif os.path.isdir(m):
                # Like Docker, a directory source copies its contents.
                shutil.copytree(m, dest, dirs_exist_ok=True, symlinks=True)
            else:
                target = os.path.join(dest, os.path.basename(m)) if to_dir or os.path.isdir(dest) else dest
                os.makedirs(os.path.dirname(target) or "/", exist_ok=True)
                shutil.copy2(m, target)
                _chmod(target, mode)


def _chmod(path, mode):
    if mode is not None:
        os.chmod(path, mode)


def _is_tar(path):
    try:
        return tarfile.is_tarfile(path)
    except OSError:
        return False


def _safe_extract(t, dest):
    dest = os.path.realpath(dest)
    for member in t.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if target != dest and not target.startswith(dest + os.sep):
            raise Error(f"refusing to extract {member.name} outside {dest}")
    try:
        t.extractall(dest, filter="fully_trusted")
    except TypeError:
        t.extractall(dest)


# --- features: fetching -----------------------------------------------------------------

def http_get(url, headers=None, unredirected=None):
    req = urllib.request.Request(url, headers=headers or {})
    # Credentials must not follow a redirect to blob storage on another host.
    for k, v in (unredirected or {}).items():
        req.add_unredirected_header(k, v)
    return urllib.request.urlopen(req, timeout=120)


class Registry:
    """Minimal anonymous OCI distribution client (token auth per challenge)."""

    ACCEPT = "application/vnd.oci.image.manifest.v1+json"

    def __init__(self, host):
        self.host = host
        self.tokens = {}

    def _get(self, path, repo, accept=None):
        url = f"https://{self.host}/v2/{repo}/{path}"
        headers = {"Accept": accept} if accept else {}
        token = self.tokens.get(repo)
        try:
            return http_get(url, headers, {"Authorization": f"Bearer {token}"} if token else None)
        except urllib.error.HTTPError as e:
            if e.code != 401 or token:
                raise
            challenge = e.headers.get("WWW-Authenticate", "")
        self.tokens[repo] = self._token(challenge, repo)
        return self._get(path, repo, accept)

    def _token(self, challenge, repo):
        m = re.match(r"(?i)bearer\s+(.*)", challenge)
        if not m:
            raise Error(f"{self.host}: unsupported auth challenge {challenge!r}")
        params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        query = {"scope": params.get("scope", f"repository:{repo}:pull")}
        if "service" in params:
            query["service"] = params["service"]
        with http_get(params["realm"] + "?" + urllib.parse.urlencode(query)) as r:
            body = json.load(r)
        return body.get("token") or body.get("access_token")

    def manifest(self, repo, reference):
        with self._get(f"manifests/{reference}", repo, self.ACCEPT) as r:
            data = r.read()
            digest = r.headers.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(data).hexdigest()
        return json.loads(data), digest

    def blob(self, repo, digest):
        with self._get(f"blobs/{digest}", repo) as r:
            data = r.read()
        algo, _, want = digest.partition(":")
        if algo == "sha256" and hashlib.sha256(data).hexdigest() != want:
            raise Error(f"{repo}@{digest}: blob checksum mismatch")
        return data


class FeatureRef:
    def __init__(self, ref, cfg_dir):
        self.ref = ref
        self.cfg_dir = cfg_dir
        if ref.startswith(("./", "../")):
            self.kind = "local"
            self.path = os.path.normpath(os.path.join(cfg_dir, ref))
            self.canonical = self.path
            self.id = os.path.basename(self.path)
        elif re.match(r"^https?://", ref):
            self.kind = "tarball"
            self.canonical = ref
            name = os.path.basename(urllib.parse.urlparse(ref).path)
            self.id = re.sub(r"^devcontainer-feature-|\.t(ar\.)?gz$", "", name)
        else:
            self.kind = "oci"
            m = re.match(r"^([^/]+)/(.+?)(?:(@sha256:[0-9a-f]{64})|:([\w][\w.-]*))?$", ref)
            if not m:
                raise Error(f"cannot parse feature reference {ref!r}")
            self.registry, self.repo = m.group(1), m.group(2)
            self.reference = (m.group(3) or "")[1:] or m.group(4) or "latest"
            self.canonical = f"{self.registry}/{self.repo}"
            self.id = self.repo.rsplit("/", 1)[-1]

    def __repr__(self):
        return self.ref


def fetch_feature(fref, lock, registries):
    """Returns (directory, source identity) for a feature reference."""
    if fref.kind == "local":
        if not os.path.isfile(os.path.join(fref.path, "devcontainer-feature.json")):
            raise Error(f"{fref.ref}: no devcontainer-feature.json in {fref.path}")
        return fref.path, "local:" + tree_digest(fref.path)
    os.makedirs(CACHE_DIR, exist_ok=True)
    if fref.kind == "tarball":
        dest = os.path.join(CACHE_DIR, "tarball-" + sha256(fref.ref)[:16])
        with http_get(fref.ref) as r:
            data = r.read()
        _extract_feature(data, dest)
        return dest, "tarball:" + hashlib.sha256(data).hexdigest()

    locked = (lock.get(fref.ref) or {}).get("resolved")
    reference = fref.reference
    if locked and "@sha256:" in locked:
        reference = locked.split("@", 1)[1]
    registry = registries.setdefault(fref.registry, Registry(fref.registry))
    try:
        manifest, digest = registry.manifest(fref.repo, reference)
        layers = [l for l in manifest.get("layers", [])
                  if l.get("mediaType", "").startswith("application/vnd.devcontainers.layer")]
        if not layers:
            raise Error(f"{fref.ref}: manifest has no devcontainer feature layer")
        data = registry.blob(fref.repo, layers[0]["digest"])
        dest = os.path.join(CACHE_DIR, digest.replace(":", "-"))
        _extract_feature(data, dest)
        return dest, "oci:" + digest
    except (urllib.error.URLError, OSError, Error) as e:
        if fref.registry != "ghcr.io":
            raise Error(f"{fref.ref}: {e}") from e
        return _fetch_from_github(fref, e)


def _fetch_from_github(fref, cause):
    """ghcr.io/OWNER/REPO/ID is published by the feature template from
    github.com/OWNER/REPO, directory src/ID."""
    parts = fref.repo.split("/")
    if len(parts) < 3:
        raise Error(f"{fref.ref}: {cause}")
    owner, repo, fid = parts[0], parts[1], parts[-1]
    warn(f"{fref.ref}: registry download failed ({cause}); "
         f"using src/{fid} from github.com/{owner}/{repo} (default branch, may be newer than :{fref.reference})")
    tmp = tempfile.mkdtemp(prefix="feature-", dir=CACHE_DIR)
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{owner}/{repo}.git", tmp],
                   check=True)
    src = os.path.join(tmp, "src", fid)
    if not os.path.isfile(os.path.join(src, "devcontainer-feature.json")):
        raise Error(f"{fref.ref}: github.com/{owner}/{repo} has no src/{fid}")
    head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return src, f"git:{owner}/{repo}@{head}"


def _extract_feature(data, dest):
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
        _safe_extract(t, dest)


def load_lock(cfg_path):
    d = os.path.dirname(cfg_path)
    name = ".devcontainer-lock.json" if os.path.basename(cfg_path).startswith(".") else "devcontainer-lock.json"
    path = os.path.join(d, name)
    if os.path.exists(path):
        return load_jsonc(path).get("features", {})
    return {}


# --- features: resolving and ordering ------------------------------------------------------

class Feature:
    def __init__(self, fref, options, order):
        self.fref = fref
        self.options = options
        self.order = order
        self.dir = None
        self.source = None
        self.meta = {}

    @property
    def id(self):
        return self.meta.get("id") or self.fref.id


def resolve_features(cfg, cfg_dir, ctx, skip, lock):
    """Fetches features (and their dependsOn closure), in install order."""
    registries = {}
    features = []
    seen = {}

    def add(ref, options, origin):
        fref = FeatureRef(ref, cfg_dir)
        if fref.canonical in seen:
            return seen[fref.canonical]
        if fref.id in skip:
            info(f"skipped: {ref}" + (f" (dependency of {origin})" if origin else ""))
            seen[fref.canonical] = None
            return None
        feature = Feature(fref, options if isinstance(options, dict) else {}, len(features))
        if isinstance(options, str):
            # "feature": "1.2" is shorthand for {"version": "1.2"}.
            feature.options = {"version": options}
        seen[fref.canonical] = feature
        feature.dir, feature.source = fetch_feature(fref, lock, registries)
        feature.meta = load_jsonc(os.path.join(feature.dir, "devcontainer-feature.json"))
        features.append(feature)
        for dep, dep_opts in (feature.meta.get("dependsOn") or {}).items():
            add(dep, dep_opts, ref)
        return feature

    for ref, options in (cfg.get("features") or {}).items():
        add(ref, substitute(options, ctx), None)
    return order_features(features, cfg.get("overrideFeatureInstallOrder") or [], cfg_dir)


def _base(ref, cfg_dir):
    try:
        return FeatureRef(ref, cfg_dir).canonical
    except Error:
        return ref


def order_features(features, override, cfg_dir):
    by_canonical = {f.fref.canonical: f for f in features}
    priority = {_base(ref, cfg_dir): i for i, ref in enumerate(override)}
    deps = {}
    for f in features:
        wanted = list((f.meta.get("dependsOn") or {}).keys()) + list(f.meta.get("installsAfter") or [])
        deps[f] = {by_canonical[c] for c in (_base(r, f.dir) for r in wanted)
                   if c in by_canonical and by_canonical[c] is not f}
    ordered = []
    pending = list(features)
    while pending:
        ready = [f for f in pending if deps[f] <= set(ordered)]
        if not ready:
            raise Error("feature dependency cycle: " + ", ".join(f.fref.ref for f in pending))
        ready.sort(key=lambda f: (priority.get(f.fref.canonical, len(priority)), f.order))
        ordered.append(ready[0])
        pending.remove(ready[0])
    return ordered


def feature_env(feature, ctx):
    values = {}
    for oid, spec in (feature.meta.get("options") or {}).items():
        if isinstance(spec, dict) and "default" in spec:
            values[oid] = spec["default"]
    values.update(feature.options)
    env = {}
    for oid, v in values.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        env[option_env_name(oid)] = substitute(str(v), ctx)
    home = os.path.expanduser("~")
    env.update({"_REMOTE_USER": "root", "_REMOTE_USER_HOME": home,
                "_CONTAINER_USER": "root", "_CONTAINER_USER_HOME": home})
    return env


def install_features(features, ctx, env, runner, stamps):
    for f in features:
        opts = feature_env(f, ctx)
        key = sha256(f.fref.canonical, f.source, json.dumps(opts, sort_keys=True))
        log(f"feature {f.fref.ref} ({f.source})")
        for k in FEATURE_CONTAINER_ONLY:
            if f.meta.get(k):
                info(f"{k} ignored (container-only)")
        if stamps.done(key):
            info("cached")
        else:
            if not os.path.exists(os.path.join(f.dir, "install.sh")):
                raise Error(f"{f.fref.ref}: no install.sh")
            for k, v in sorted(opts.items()):
                if not k.startswith("_"):
                    info(f"{k}={v}")
            # Like the CLI, run from a scratch copy: install.sh may write next
            # to itself, and a local feature lives in the user's checkout.
            work = f.dir
            if not runner.dry_run:
                os.makedirs(CACHE_DIR, exist_ok=True)
                work = tempfile.mkdtemp(prefix=f"install-{f.id}-", dir=CACHE_DIR)
                shutil.copytree(f.dir, work, dirs_exist_ok=True, symlinks=True)
                with open(os.path.join(work, "devcontainer-features.env"), "w") as fh:
                    fh.writelines(f'{k}="{_dq_escape(v)}"\n' for k, v in sorted(opts.items()))
            try:
                runner.argv(["/bin/sh", "-c", "chmod +x ./install.sh && ./install.sh"],
                            env.environ(opts), work, display=f"{f.id}/install.sh")
            finally:
                if work != f.dir:
                    shutil.rmtree(work, ignore_errors=True)
            stamps.mark(key, f.fref.ref)
        apply_env(f.meta.get("containerEnv") or {}, env, ctx)


def apply_env(mapping, env, ctx, container_env=False):
    for k, v in mapping.items():
        v = substitute(str(v), ctx, env.environ() if container_env else None)
        env.set(k, v, expand_shell_vars(v, env.environ()))


# --- lifecycle commands ---------------------------------------------------------------------

def probe_env(mode, env, dry_run):
    """Like the CLI's userEnvProbe: picks up what features wrote to shell rc
    files (e.g. OPAMROOT in /etc/bash.bashrc), which lifecycle commands rely on.
    Returns the variables that differ from the tool's own environment."""
    flags = PROBE_FLAGS.get(mode)
    if not flags or dry_run:
        return {}
    shell = next((s for s in (os.environ.get("SHELL"), "/bin/bash") if s and os.access(s, os.X_OK)), None)
    if not shell or os.path.basename(shell) not in ("bash", "zsh"):
        return {}
    base = env.environ()
    try:
        r = subprocess.run([shell] + flags + ["-c", "env -0"], env=base, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        warn(f"userEnvProbe failed: {e}")
        return {}
    probed = {}
    for entry in r.stdout.split(b"\0"):
        k, eq, v = entry.decode(errors="replace").partition("=")
        if eq and k and base.get(k) != v and k not in ("_", "SHLVL", "PWD", "OLDPWD", "PS1"):
            probed[k] = v
    return probed

def run_lifecycle(name, commands, ctx, env, runner, cwd):
    """commands: list of (origin, command) in execution order."""
    for origin, command in commands:
        if command in (None, "", [], {}):
            continue
        log(f"{name} ({origin})")
        command = substitute(command, ctx, env.environ())
        items = command.items() if isinstance(command, dict) else [(None, command)]
        for label, cmd in items:
            if label:
                info(f"--- {label}")
            if isinstance(cmd, list):
                runner.argv([str(c) for c in cmd], env.environ(), cwd)
            elif cmd:
                runner.shell(str(cmd), env.environ(), cwd)


# --- main ------------------------------------------------------------------------------------

def find_config(workspace, config):
    if config:
        path = config if os.path.isabs(config) else os.path.join(workspace, config)
        if not os.path.isfile(path):
            raise Error(f"{path}: no such file")
        return os.path.normpath(path)
    for candidate in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        path = os.path.join(workspace, candidate)
        if os.path.isfile(path):
            return path
    others = sorted(glob.glob(os.path.join(workspace, ".devcontainer", "*", "devcontainer.json")))
    if others:
        listing = ", ".join(os.path.relpath(p, workspace) for p in others)
        raise Error(f"no default configuration; pick one with --config: {listing}")
    raise Error(f"no devcontainer configuration in {workspace}")


def main(argv=None):
    p = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    p.add_argument("--workspace-folder", default=os.getcwd(),
                   help="project root (default: current directory)")
    p.add_argument("--config", help="devcontainer.json path, relative to the workspace folder")
    p.add_argument("--skip-feature", action="append", default=[], metavar="ID",
                   help="feature id to skip (repeatable); also DEVCONTAINER_HOST_SKIP")
    p.add_argument("--no-default-skips", action="store_true",
                   help=f"also install {', '.join(DEFAULT_SKIP)}")
    p.add_argument("--skip-dockerfile", action="store_true", help="do not replay the Dockerfile")
    p.add_argument("--skip-features", action="store_true", help="do not install features")
    p.add_argument("--skip-lifecycle", action="store_true", help="do not run lifecycle commands")
    p.add_argument("--env-file", metavar="FILE",
                   help="also write the resulting environment (config variables plus what "
                        "userEnvProbe finds in shell rc files) to FILE as export lines, for "
                        "shells that read neither /etc/profile.d nor bash.bashrc")
    p.add_argument("--force", action="store_true", help="ignore stamps from earlier runs")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and fetch everything, print the plan, change nothing")
    args = p.parse_args(argv)

    try:
        return _main(args)
    except Error as e:
        print(f"{PROG}: error: {e}", file=sys.stderr)
        return 1


def _main(args):
    if not args.dry_run and os.geteuid() != 0:
        raise Error("must run as root (it installs system-wide, like a container build)")
    workspace = os.path.realpath(args.workspace_folder)
    cfg_path = find_config(workspace, args.config)
    cfg_dir = os.path.dirname(cfg_path)
    cfg = load_jsonc(cfg_path)
    ctx = {
        "localWorkspaceFolder": workspace,
        "localWorkspaceFolderBasename": os.path.basename(workspace),
        "containerWorkspaceFolder": workspace,
        "containerWorkspaceFolderBasename": os.path.basename(workspace),
        "devcontainerId": sha256(cfg_path)[:16],
    }
    log(f"{cfg.get('name', 'devcontainer')}: {os.path.relpath(cfg_path, workspace)}"
        + (" (dry run)" if args.dry_run else ""))
    if cfg.get("dockerComposeFile"):
        raise Error("Docker Compose configurations are not supported")
    for k in CONTAINER_ONLY:
        if k in cfg:
            info(f"{k} ignored (container-only)")

    # Build-time variables in the config (build.args, feature options) are
    # expanded up front; ${containerEnv:...} waits until commands run.
    for key in ("build", "image", "containerEnv", "remoteEnv"):
        if key in cfg:
            cfg[key] = substitute(cfg[key], ctx)

    skip = set() if args.no_default_skips else set(DEFAULT_SKIP)
    skip.update(args.skip_feature)
    skip.update(os.environ.get("DEVCONTAINER_HOST_SKIP", "").split())

    runner = Runner(args.dry_run)
    stamps = Stamps(sha256(cfg_path)[:16], force=args.force, dry_run=args.dry_run)
    env = Env()

    if not args.skip_lifecycle:
        # On the host already; the CLI runs this before building.
        run_lifecycle("initializeCommand", [("config", cfg.get("initializeCommand"))],
                      ctx, env, runner, workspace)

    if not args.skip_dockerfile:
        replay_dockerfile(cfg, cfg_dir, ctx, env, runner, stamps)
    elif cfg.get("image"):
        report_base_image(cfg["image"])

    features = []
    if not args.skip_features:
        log("resolving features")
        features = resolve_features(cfg, cfg_dir, ctx, skip, load_lock(cfg_path))
        info("install order: " + (", ".join(f.fref.ref for f in features) or "(none)"))
        install_features(features, ctx, env, runner, stamps)

    apply_env(cfg.get("containerEnv") or {}, env, ctx)
    apply_env(cfg.get("remoteEnv") or {}, env, ctx, container_env=True)
    if env.assignments:
        log(f"environment -> {PROFILE_SCRIPT}")
        for k, raw in env.assignments:
            info(f"{k}={raw}")
        if not args.dry_run:
            env.write_profile()

    probed = probe_env(cfg.get("userEnvProbe", "loginInteractiveShell"), env, args.dry_run)
    for k, v in probed.items():
        if k not in env.values:
            env.values[k] = v
    if args.env_file and not args.dry_run:
        exported = {k: v for k, v in env.environ().items() if os.environ.get(k) != v}
        with open(args.env_file, "w") as f:
            f.writelines(f'export {k}="{_dq_escape(v).replace("$", chr(92) + "$")}"\n'
                         for k, v in sorted(exported.items())
                         if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k))
        info(f"environment written to {args.env_file}")

    if not args.skip_lifecycle:
        for name in LIFECYCLE:
            commands = [(f"feature {f.id}", f.meta.get(name)) for f in features]
            commands.append(("config", cfg.get(name)))
            run_lifecycle(name, commands, ctx, env, runner, workspace)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
