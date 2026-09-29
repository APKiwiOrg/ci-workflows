# ci-workflows contributor instructions

This public repository holds the reusable GitHub Actions workflows every APKiwiOrg repository calls. The
README says how to call each one. This file holds the rules for changing them.

## Rules

- Every job runs on a GitHub-hosted runner, pinned to an explicit image (`ubuntu-26.04`), never
  `ubuntu-latest` and never self-hosted. The repository is public, so a pull request could run code on a
  self-hosted runner, and the org's self-hosted runners are the dev Mac. `scripts/check-workflows.sh` enforces
  it in the pre-commit hook and in CI.
- Every third-party action and reusable workflow is pinned to a full commit SHA with its version in a
  trailing comment. The same check enforces it.
- Workflow inputs reach scripts only through `env`, never by expression interpolation into a `run` line.
- A load or stress run needs the owner's explicit permission before it starts. Ask first, as its own message,
  naming what runs, where, how many iterations and roughly how long. Never run one on the dev Mac.
- Callers pin this repository by commit SHA. A change here reaches a caller only when that caller moves its
  pin, so a breaking change to inputs names the callers to update in the same piece of work.
- The callers are the game repositories through game-template (`.github/workflows/stress-test.yml` there is
  template-managed) and KhaozEngine directly.

## Work and finish

- Work in a git worktree on a `feature/*` or `fix/*` branch. Merge the verified branch to `main` and push.
- Commit subjects use `area(scope): summary`. No em dashes, en dashes or prose semicolons in shipped text.
- Verify with `sh scripts/check-workflows.sh`, `sh scripts/check-agent-instructions.sh` and a YAML parse of
  every workflow. A behaviour change to a reusable workflow is proven by one caller run with a small input,
  for example one iteration and no load, before callers move their pins.

## Discovered work

Search prior art with `scripts/ledger.sh search <term>`, then file a GitHub issue with one `confidence/*`
label. Cross-repository handoffs use a full issue URL.
