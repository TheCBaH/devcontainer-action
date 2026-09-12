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
