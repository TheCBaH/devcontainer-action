# devcontainer-action

Composite GitHub Action that builds, layer-caches, and (on push) publishes a
devcontainer image on an Ubuntu runner, and emits an `exec` prefix for
running commands inside it.

## Usage

```yaml
- name: devcontainer
  id: devcontainer
  uses: TheCBaH/devcontainer-action/devcontainer@v1
  with:
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}
- run: ${{ steps.devcontainer.outputs.exec }} make test
```

See [`devcontainer/action.yml`](devcontainer/action.yml) for the full input
list (`name`, `variant`, `platform`, `config`, `registry`, `username`,
`password`, `post-create`, `cli-version`) and their defaults.

### Recommended: separate build/publish and consume jobs

Run two workflows/jobs against the same action:

- A **publish** job (e.g. `images.yml`, triggered on push) that sets
  `username`/`password` so it builds *and pushes* the image.
- A **consume** job (e.g. `build.yml`'s test matrix, triggered on push and
  PRs) that omits `username`/`password` entirely. It still runs
  `devcontainer build`, but `--cache-from` pulls the layers the publish job
  already pushed, so it's fast and never needs registry write access —
  important for jobs that also run on pull requests from forks.

`username`/`password` are declared `required: true` only as documentation;
composite actions don't enforce it, so omitting both is how a consume-only
job opts out of pushing.

## History

Extracted from `TheCBaH/ocaml-devcontainer`, where this action originated
and where several other repos had each copied and independently drifted a
version of it. See `devcontainer-action.md` in `TheCBaH/err_trace` for the
design behind the move.

## Testing

`test/fixture` is a minimal devcontainer (`mcr.microsoft.com/devcontainers/base:debian`,
no features) used by `.github/workflows/test.yml` so CI exercises the
action's own logic — build, cache reuse, `post-create`, `cli-version`,
`config` — without paying for a real consumer's build.

## Without Docker: `host/devcontainer_host.py`

Applies a `devcontainer.json` directly to the current machine, for
environments that have no Docker but should match the devcontainer, such as
Claude Code cloud environments or a throwaway VM. It is a single Python 3
file with no dependencies beyond the standard library.

```sh
curl -fsSL https://raw.githubusercontent.com/TheCBaH/devcontainer-action/main/host/devcontainer_host.py |
    sudo python3 - --workspace-folder path/to/project
```

It replays, as root and in this order:

1. **The Dockerfile's target stage**: `ARG`, `ENV`, `WORKDIR`, `SHELL`, `RUN`
   (shell, exec and heredoc forms), `COPY`/`ADD` from the build context, with
   `build.args` and `build.target` applied. `FROM` is not pulled, so the host
   stands in for the base image; a Debian image on an Ubuntu host is reported.
   For an `image`-only configuration there is nothing to replay.
2. **Features**, from an OCI registry (anonymous token auth, honouring
   `devcontainer-lock.json`), a local directory or a tarball URL, in
   `dependsOn`/`installsAfter`/`overrideFeatureInstallOrder` order. Options are
   exported to `install.sh` the way the CLI does, and each feature's
   `containerEnv` applies to everything after it. When a `ghcr.io` download
   fails, the feature's source is taken from the matching GitHub repository
   (`ghcr.io/OWNER/REPO/ID` -> `github.com/OWNER/REPO`, `src/ID`).
3. **Lifecycle commands**: `initializeCommand`, then `onCreateCommand`,
   `updateContentCommand`, `postCreateCommand`, `postStartCommand` and
   `postAttachCommand`, each features' before the config's own, with the
   environment `userEnvProbe` finds in shell rc files, as in a container.

Supported variables: `${localEnv:VAR[:default]}`, `${containerEnv:VAR[:default]}`,
`${localWorkspaceFolder}`, `${containerWorkspaceFolder}` (the same folder
here), their `Basename` forms and `${devcontainerId}`.

### Host differences

- Everything runs as root. `remoteUser`, `USER` and the container user are
  reported and ignored, and `sudo` in lifecycle commands still works where
  sudo is not installed.
- Skipped by default: `common-utils` (creates the container user),
  `docker-outside-of-docker` and `docker-in-docker`. Use `--skip-feature ID`
  for more, or `--no-default-skips`.
- `userdel`, `deluser`, `groupdel` and `delgroup` are no-ops in the scripts
  this tool runs: Dockerfiles often remove the base image's default user,
  which on a host is a real account.
- Container-only settings (`runArgs`, `mounts`, `privileged`, `capAdd`,
  ports, `hostRequirements`, ...) are reported and ignored. Docker Compose
  configurations and `COPY --from` are not supported.
- Environment from `ENV`, `containerEnv` and `remoteEnv` is written to
  `/etc/profile.d/devcontainer-host.sh`, which `/etc/bash.bashrc` also
  sources. `--env-file FILE` additionally writes the full resulting
  environment (including what features put in shell rc files), for tools
  whose shells read neither.

### Reruns

Each Dockerfile step is stamped under `/var/lib/devcontainer-host`, keyed on
everything before it, like Docker's layer cache; each feature is stamped by
its source digest and options. A rerun skips what is unchanged and runs the
lifecycle commands again. `--force` ignores the stamps; `--dry-run` resolves
and fetches everything, prints the plan and changes nothing.

### Claude Code cloud environments

Set the environment's setup script to provision whichever project the
session cloned:

```sh
#!/bin/bash
set -euo pipefail
curl -fsSL https://raw.githubusercontent.com/TheCBaH/devcontainer-action/main/host/devcontainer_host.py \
    -o /tmp/devcontainer_host.py
for dir in /home/user/*/; do
    if [ -f "$dir.devcontainer/devcontainer.json" ] || [ -f "$dir.devcontainer.json" ]; then
        python3 /tmp/devcontainer_host.py --workspace-folder "$dir"
    fi
done
```

The environment's network access has to allow what the Dockerfile and
features download (for example `ghcr.io` and `pkg-containers.githubusercontent.com`
for features, `opam.ocaml.org` for the OCaml feature).

`test/host` holds the unit tests and an integration fixture:

```sh
sudo python3 test/host/test_devcontainer_host.py
```
