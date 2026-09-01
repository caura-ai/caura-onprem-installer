# Legacy-name engine parity gate

The fleet vendors `scripts/legacy_name_ratchet.py`, but `caura` owns the canonical engine. The
`Legacy-name engine parity` required workflow checks the candidate revision's vendored file
byte-for-byte against the copy fetched from `caura/main`. It fails closed when the local file is
missing, is a symlink, differs, or the canonical copy cannot be fetched.

The expected bytes are independent of the file under test: the checker downloads
`https://raw.githubusercontent.com/caura-ai/caura/main/scripts/legacy_name_ratchet.py` into a
temporary directory. It never executes either Python file.

## Why the workflow lives here

`caura-onprem-installer` is public. GitHub permits a public required workflow to run in any public
or private repository in the organization, while the private `caura-ai/.github` workflow source
cannot run here. Keeping the one enforcement copy in this already-public fleet repository covers
all seven visibility-compatible targets without exposing the private `.github` repository or
duplicating the checker.

In a ruleset run, the workflow checks out the target candidate and its own source into separate
directories. The latter is pinned to `job.workflow_repository` and `job.workflow_sha`: the exact
repository and immutable commit that supplied the required job. The target candidate cannot change
the checker that evaluates it. The existing shared review workflow and its release tag are not
involved.

## Enforcement

After this workflow is merged to `caura-onprem-installer/main`, create one organization ruleset in
**Evaluate** mode:

- Select exactly `caura-enterprise`, `openclaw-fleet-tester`, `caura-ops`,
  `caura-onprem-installer`, `caura-daemon`, `caura-onprem`, and `caura-test-automation`.
- Under **Target branches**, include only **Default branch** (`~DEFAULT_BRANCH`). This selects
  enterprise's `dev` and the other six repositories' `main` without also targeting enterprise's
  separate `main` branch.
- Require `.github/workflows/legacy-name-engine-parity.yml` from
  `caura-ai/caura-onprem-installer` on `main`.
- Do not add repository or path filters to the workflow. The ruleset is the target boundary, and
  the workflow must also run for merge queues through `merge_group`.

Keep the parity job's explicit event condition. GitHub requires an explicit job condition when an
enabled `pull_request` workflow is also used as a ruleset workflow outside its source repository;
without it, cross-repository `pull_request` ruleset runs are not scheduled.

Ruleset workflows are not added retroactively to pull requests that were already open when the
rule was created. Open a validation pull request, push a new commit to one, or close and reopen one
in each of the seven targets. Verify all seven Evaluate runs are green in Rule Insights before
changing the ruleset to **Active**.

The source workflow's behavioral test deliberately changes one byte without changing file length
and requires the checker to exit 1. Every future merge that changes this workflow, checker, or test
becomes the source for subsequent fleet runs. Validate those changes in their pull request. If a
central change breaks the fleet, revert that source commit; disable the ruleset only as a temporary
incident measure. Evaluate mode stages the initial rollout, not later source changes.

A legitimate change to the canonical engine on `caura/main` intentionally makes all seven checks
red until the new engine is re-ported and each repository's zero-movement proof is reproduced.

## Scope and remediation

This gate covers only `scripts/legacy_name_ratchet.py`. It does not compare the ratchet config,
allowlist, sentinel, tests, workflow files, reports, or any other vendored file.

On a mismatch, copy the canonical engine into the fleet repository unchanged. Then run that
repository's ratchet tests, gate/report, sentinel, and exact CI gates, and prove the report has
zero added, annotated, removed, moved, and net movement. Do not edit the config, allowlist,
markers, or counted content to force a green result.
