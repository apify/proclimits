# Contributing

## Set up

The project is managed with [uv](https://docs.astral.sh/uv/). Install it, then install the development
dependencies:

```sh
uv run poe install-dev
```

Every task below runs through [poe](https://poethepoet.natn.io/):

| Task | What it does |
| --- | --- |
| `uv run poe lint` | Checks formatting and runs ruff |
| `uv run poe format` | Applies ruff fixes and reformats |
| `uv run poe type-check` | Runs ty |
| `uv run poe unit-tests` | Runs the unit tests |
| `uv run poe e2e-tests` | Runs the end-to-end tests |
| `uv run poe check-code` | Runs lint, type check and unit tests |
| `uv run poe build` | Builds the sdist and the wheel |

## Tests

The unit tests run anywhere. `uv run poe check-code` covers what usually breaks, but it is not everything a
pull request runs: CI repeats the unit tests on five interpreters across Linux and Windows, runs the whole
end-to-end suite, and checks the pull request title.

The end-to-end tests read the machine they run on, so what they can check depends on that machine. Tests
whose environment is missing skip themselves. Four markers name the environments that are not universal:

| Marker | Needs |
| --- | --- |
| `unified` | The resource controllers on the cgroup v2 unified hierarchy |
| `systemd_slices` | An engine that places containers under systemd slices |
| `kubernetes` | `kind` and `kubectl` |
| `podman` | `podman` with a systemd user manager delegating cpu and memory |

CI runs the suite across separate lanes, including two Ubuntu guests booted under QEMU for cgroup v1, which
no GitHub runner offers. `tests/e2e/scripts/guest.sh` boots those guests and runs the suite inside them; it
is the same script CI calls.

## Pull requests

Squash merging is the only way a pull request lands, and the squashed commit takes its subject from the
pull request title. That title is therefore the line that ends up in the changelog, so it has to follow
[conventional commits](https://www.conventionalcommits.org/):

```text
fix(windows): Read the job object limit through a fresh handle
```

CI checks the title on every pull request and fails on one that does not parse.

## Releases

Releases are cut by hand. Somebody dispatches the `Publish` workflow, which derives the version from the
conventional-commit subjects since the last tag, writes `CHANGELOG.md` and `pyproject.toml`, tags the
commit, and publishes to PyPI. Nothing is published on a merge, and there is no pre-release stream.

Do not merge anything while a release is running. The workflow waits for the end-to-end lanes, which take
the better part of an hour, and the commit it publishes is rebased onto the branch tip at the moment it
writes the version. A pull request landing inside that window is published without ever appearing in the
changelog.
