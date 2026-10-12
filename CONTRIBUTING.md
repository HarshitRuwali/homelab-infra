# Contributing

How changes get in, how commit messages drive versions, and how a release
happens. The repository is public: never commit a secret, an inventory or a
credential (see [Secrets](README.md#secrets)).

## Branches and pull requests

Nothing is pushed to `master`. Work on a branch, open a pull request, and the
repository owner merges it. Every change goes through a PR, including docs.

## Commit messages

Commits and PR titles follow [Conventional Commits](https://www.conventionalcommits.org):

```text
type(scope): description
```

The scope is the module directory, lowercase: `feat(monitoring): add a disk
alert`, `fix(s3-backup): handle an empty bucket`. Leave it out when a change is
not about one module. Add `!` before the colon (`feat(ansible)!: drop the old
inventory format`), or a `BREAKING CHANGE:` footer, for a breaking change.

| Type | Changelog section | Use it for |
|---|---|---|
| `feat` | Features | a new capability |
| `fix` | Bug Fixes | a bug fix |
| `perf` | Performance | a speed or resource improvement |
| `refactor` | Refactoring | restructuring with no behavior change |
| `docs` | Documentation | documentation only |
| `revert` | Reverts | undoing an earlier commit |
| `test` | hidden | tests |
| `build` | hidden | build and packaging |
| `ci` | hidden | workflows and CI scripts |
| `chore` | hidden | everything else |
| `style` | hidden | formatting only |

Hidden types do not appear in a changelog, and a module whose only new commits
are hidden gets no release. Marking one breaking (`chore!:`) overrides that. If
a change should reach users, use a visible type.

### PR titles are checked

`.github/workflows/checks.yml` rejects a PR whose title is not a conventional
commit. The title is the one that matters: with a squash merge it becomes the
commit subject on `master`, and release-please reads that subject. Run
`.github/scripts/conventional-commits.sh --title "feat(monitoring): x"` to try a
title locally.

GitHub's revert button titles a PR `Revert "feat: x"`. The check accepts that,
but release-please does not recognise it, so the revert would be missing from
the changelog. Retitle it `revert: x` if it should be listed.

### Merge method

Squash merge is recommended: one PR becomes one commit and one changelog line,
whatever the branch commits looked like. Check that the subject GitHub proposes
in the merge dialog is the PR title.

A plain merge also works, but then every commit on the branch is what
release-please sees, so each one must be conventional. The check posts a
warning for each branch commit that is not; the warning does not fail the PR.

## Versioning

Each module has its own [SemVer](https://semver.org) version, because the
modules stand alone and several began as separate repositories. There is no
repository-wide version.

- **Tag:** `<module>-vX.Y.Z`, for example `monitoring-v0.2.0`.
- **GitHub release:** one per module release, created when its tag is.
- **Changelog:** `CHANGELOG.md` inside the module directory, written by
  release-please.
- **Which module a commit belongs to:** release-please looks at the paths a
  commit touches, not at its scope. A commit that touches two modules counts for
  both. A commit that touches only root files (`README.md`, `.github/`,
  `CONTRIBUTING.md`) belongs to no module and releases nothing. A commit that
  changes no files at all counts for every module, so avoid
  `git commit --allow-empty`.
- **Bumps:** any visible type releases the module it touches. `feat` bumps the
  minor and every other visible type (`fix`, `perf`, `refactor`, `docs`,
  `revert`) bumps the patch, so a `docs(monitoring):` commit does release
  monitoring. A breaking change bumps the major. Below 1.0.0 a breaking change
  bumps the minor instead, so a `0.x` module never jumps to 1.0.0 by accident.
  Reach 1.0.0 on purpose with `Release-As` (below).
- **Version strings inside a module** (a `pyproject.toml`, `uv.lock`, a
  `plugin.yaml`, a `__version__`) are updated by the release PR together with
  the changelog. Do not edit them by hand. Where they are is listed under
  `extra-files` in `release-please-config.json`.

The starting version of each module is its `initial-version` in
`release-please-config.json`. A module's first release is exactly that version,
whatever the commit types, and later releases bump from it.

## Releasing

Releases are cut by `.github/workflows/release.yml`. Nobody tags by hand.

1. Conventional commits land on `master`.
2. release-please opens, and then keeps updating, one pull request titled
   `chore: release master`. It lists every module with pending changes: the
   new version, the changelog entry and the version-string edits.
3. When the owner merges it, release-please creates each module's tag and
   publishes its GitHub release.

Leave the release PR open as long as you like. It grows as more PRs land, and a
module with nothing releasable is not in it.

### Forcing a version

Add a `Release-As: X.Y.Z` footer to a commit that touches that module:

```text
feat(memory): stable API

Release-As: 1.0.0
```

For a squash merge, the footer has to be in the body of the squash commit, so
check the merge dialog. Do not put it on an empty commit: a commit with no files
counts for every module, so the footer would force that version on all of them.

### Adding a module

1. Create the directory with a `README.md`, and add a row to the Modules table
   in the root `README.md`.
2. Add a package for it in `release-please-config.json`: `component` set to the
   directory name, `changelog-path` set to `CHANGELOG.md`, and an
   `initial-version`.
3. If a file holds the module's own version, put `# x-release-please-version`
   on that line and list the file under `extra-files`. A bare string works for
   source files such as `.py`. For `.yaml`, `.yml`, `.json`, `.toml` and `.xml`
   files, use `{"type": "generic", "path": "..."}`: a bare string with one of
   those extensions is parsed and re-written whole, which drops the comment. To
   edit a value in a TOML file by key, use `{"type": "toml", "path": "...",
   "jsonpath": "..."}`. For `uv.lock`, match the project's own entry by name,
   and filter on `@.name.value`, not `@.name`.

`.github/scripts/release-config-test.sh` fails until the directory, the README
table and the config agree.

## Deploying a pinned version

A tag pins the whole repository at the moment of that release. To deploy one
module at a known version, check out its tag and deploy from that module's
directory:

```bash
git fetch --tags
git checkout monitoring-v0.2.0          # detached HEAD; the other modules are at that commit too
# or leave your working copy alone and use a second checkout:
git worktree add ../homelab-infra-monitoring-v0.2.0 monitoring-v0.2.0
```

Use only the module you tagged. The other directories are at whatever state
they had then, not at their own latest release.

## One-time repository setting

The release workflow opens a pull request with the built-in `GITHUB_TOKEN`, which
GitHub forbids unless this is on (an admin setting, so it cannot be automated):

**Settings** > **Actions** > **General** > **Workflow permissions** > **Allow
GitHub Actions to create and approve pull requests**

Until it is on, the Release workflow fails with "GitHub Actions is not
permitted to create or approve pull requests".

## Why the release PR shows no checks

A pull request or tag created with `GITHUB_TOKEN` does not trigger other
workflows, so `chore: release master` has no checks and no docs build. It only
edits `CHANGELOG.md` files and version strings, and the things that could go
wrong are guarded on every other PR and on `master`:

- `.github/scripts/release-config-test.sh` asserts that the release config, the
  manifest, the module directories, the README table and the version in each
  tracked file all agree. A hand edit to a version string fails it.
- `.github/scripts/conventional-commits-test.sh` tests the commit-message check
  itself.

Both run in `checks.yml` and can be run locally from the repository root. The
config test needs Python 3.11 or newer and nothing else.
