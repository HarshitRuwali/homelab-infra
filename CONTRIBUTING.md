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

The PR title becomes the changelog entry, so write it for someone reading the
changelog: what changed, in plain words, not how or why. The text of a `BREAKING
CHANGE:` footer is what the changelog shows under its breaking changes heading, so
say there what to do about it.

| Type | Release notes section | Use it for |
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

Hidden types do not appear in the release notes, and a module whose only new
commits are hidden gets no release. Marking one breaking (`chore!:`) overrides
that. If a change should reach users, use a visible type.

### PR titles are checked

`.github/workflows/checks.yml` rejects a PR whose title is not a conventional
commit. The title is the one that matters: with a squash merge it becomes the
commit subject on `master`, and release-please reads that subject. Run
`.github/scripts/conventional-commits.sh --title "feat(monitoring): x"` to try a
title locally.

GitHub's revert button titles a PR `Revert "feat: x"`. The check accepts that,
but release-please does not recognise it, so the revert would be missing from
the release notes. Retitle it `revert: x` if it should be listed.

The same job reports what the PR does to module versions: a table in the job
summary with the bump each touched module gets from the title, and a warning when
a module changes under a hidden type (so nothing is released) or the title scope
names a module the PR does not touch. It only reports: what a title or a file
says never fails the PR. Footers such as `BREAKING CHANGE:` or `Release-As:` in
the squash commit body are not visible to it and can change the result. Run
`.github/scripts/versioning-impact.sh --title "feat(monitoring): x" origin/master HEAD`
to see the report for your branch.

### Merge method

Use **Squash and merge**, with the PR title as the commit subject. One PR then
becomes one commit on `master` and one line in the release notes. Check that the
subject in the merge dialog is the PR title.

Plain merge commits and rebase merges are not supported. release-please splits a
commit message at a blank line followed by `type(scope): `, and GitHub writes the
PR title into the body of a merge commit. A plain merge therefore lists the PR
twice (once for the branch commit, once for the merge commit), and the title's
type can raise the version bump. A rebase merge never uses the PR title at all.
A branch commit dated before the last release but merged after it can also be
missed.

Recommended repository settings (Settings > General > Pull Requests): allow
squash merging only, with the default commit message set to **Pull request
title**. Otherwise GitHub proposes the commit subject, not the checked PR title,
for a single-commit PR.

The check posts a warning for each branch commit that is not conventional. Those
warnings matter only for a non-squash merge; with a squash merge the branch
commits never reach `master`.

## Versioning

Each module has its own [SemVer](https://semver.org) version, because the
modules stand alone and several began as separate repositories. There is no
repository-wide version.

- **Tag:** `<module>-vX.Y.Z`, for example `monitoring-v0.2.0`.
- **GitHub release:** one per module release, created when its tag is.
- **Release notes:** written by release-please from the squash commit titles and
  published twice: on the module's GitHub release, and in `<module>/CHANGELOG.md`,
  which the release PR edits. Each entry is a PR title with its PR link, grouped by
  type, with breaking changes first and hidden types left out. Do not edit the
  changelog by hand: fix a wrong entry by fixing the PR title before it merges.
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
- **Version strings inside a module** (a `pyproject.toml`, `uv.lock`, a
  `plugin.yaml`, a `__version__`) are updated by the release PR. Do not edit
  them by hand. Where they are is listed under `extra-files` in
  `release-please-config.json`.

Every module starts at 0.1.0, its `initial-version` in
`release-please-config.json`. A module's first release is exactly that version,
whatever the commit types, and later releases bump from it. 1.0.0 is the public
release: the owner cuts it on purpose with `Release-As` (below), and until then
every module stays on `0.x`.

Internal artifact tags are not the module version and do not follow releases.
For example, the `s3-backup-runner` image tag that `s3-backup` builds locally
stays as it is when `s3-backup` is released.

## Releasing

Releases are cut by `.github/workflows/release.yml`. Nobody tags by hand.

1. Conventional commits land on `master`.
2. release-please opens, and then keeps updating, one pull request titled
   `chore: release master`. It lists every module with pending changes: the
   new version, the release notes, the module's `CHANGELOG.md` and the
   version-string edits.
3. When the owner merges it, release-please creates each module's tag and
   publishes its GitHub release.

Leave the release PR open as long as you like. It grows as more PRs land, and a
module with nothing releasable is not in it.

The 0.1.0 release notes list each module's whole history, including old
duplicates from plain merges. They are on the GitHub releases only: a module's
`CHANGELOG.md` begins with the release after 0.1.0, so that history stays out of
it.

### Forcing a version

Add a `Release-As: X.Y.Z` footer to a commit that touches only that module:

```text
feat(memory): public release

Release-As: 1.0.0
```

The footer applies to every module the commit touches. With a squash merge that
is every module the whole PR touches, so keep that PR to the one module. The
footer has to be in the body of the squash commit, so type it into the body box
of the merge dialog. Do not put it on an empty commit: a commit with no files
counts for every module, so the footer would force that version on all of them.

### Adding a module

1. Create the directory with a `README.md`, and add a row to the Modules table
   in the root `README.md`.
2. Add a package for it in `release-please-config.json`: `component` set to the
   directory name and `initial-version` set to `0.1.0`.
3. If a file holds the module's own version, put `# x-release-please-version`
   on that line and list the file under `extra-files`. A bare string works for
   source files such as `.py`. For a `.yaml` or `.yml` file, use
   `{"type": "generic", "path": "..."}`: release-please re-dumps a bare YAML
   string whole, which drops the comment (and so the marker) and reformats the
   file. A bare `.json` string is re-dumped too, and JSON cannot hold the
   marker. A bare `.toml` or `.xml` string is edited in place, but it also
   rewrites a top-level `version` key; the config test rejects it anyway, so
   that the entry says what it edits. To edit a value in a TOML file by key, use
   `{"type": "toml", "path": "...", "jsonpath": "..."}`. For `uv.lock`, match
   the project's own entry by name, and filter on `@.name.value`, not `@.name`.

`.github/scripts/release-config-test.sh` fails until the directory, the README
table and the config agree. It runs on the pull request, so register the module
in the PR that adds it: a PR with the directory and no package fails the
`Release configuration` check. A module that is not registered never gets a
version, a tag or a release. `.github/scripts/release-config-gate-test.sh` proves
the gate by adding a module to a copy of the repository one piece at a time.

A branch cut before the release setup landed on `master` does not contain
`release-please-config.json`, so the registration cannot be added to it until
`master` is merged in. An open PR is also only checked when it gets an event (a
push or a title edit). Merge `master` into the branch, add the registration, and
push.

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
permitted to create or approve pull requests". After turning it on, re-run the
failed run, or start the Release workflow by hand from the Actions tab
(`workflow_dispatch`).

## Why the release PR shows no checks

A pull request or tag created with `GITHUB_TOKEN` does not trigger other
workflows, so `chore: release master` has no checks and no docs build. It only
edits version strings and the manifest, and the things that could go
wrong are guarded on every other PR and on `master`:

- `.github/scripts/release-config-test.sh` asserts that the release config, the
  manifest, the module directories, the README table and the version in each
  tracked file all agree. A hand edit to a version string fails it.
- `.github/scripts/conventional-commits-test.sh` tests the commit-message check
  itself.
- `.github/scripts/versioning-impact-test.sh` tests the versioning report, and
  `.github/scripts/release-config-gate-test.sh` tests that the config test
  rejects a module that is not fully registered.

They run in `checks.yml` and can be run locally from the repository root. The
config test needs Python 3.11 or newer and nothing else.

Do not make these checks required in branch protection. The release PR is opened
with `GITHUB_TOKEN`, runs no workflows, and so could never pass them.
