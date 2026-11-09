# Harness GitHub Actions CI package

This is a minimal public CI package. It contains the existing standard-library harness,
its real unit tests, and explicitly synthetic task/spec metadata. It does not include
the private X project documents, user approval records, closure reports, or their Git history.
A passing fixture lint is not acceptance of X-010 or any product behavior.

## Files needed on GitHub

| File | Purpose |
| --- | --- |
| `.github/workflows/harness.yml` | PR and main CI: lint, unit tests, merge checkout verification |
| `harness/*.py`, `harness/policy.json` | Unmodified harness runtime and registered check policy |
| `tests/test_harness.py`, `tests/test_ci_adapter.py` | Existing 42 tests plus CI-context/full-gate/evidence regression tests using temporary Git/CLI fixtures |
| `ci/verify_checkout.py`, `tests/test_ci_checkout.py` | Verify exact event SHA, PR parents, push ancestry, and public file scope |
| `docs/tasks/CI-001.json`, `docs/specs/SPEC-CI.md` | Public synthetic input for actual structural lint |
| `docs/templates/task.json` | Template required by the existing unit tests |
| `README.md`, `AGENTS.md`, `.gitignore`, `PUBLISH_MANIFEST.json` | Package scope, local entry points, and exact publication list |
| `LICENSE` | Existing remote MIT license, preserved unchanged |

The exact publication list is [PUBLISH_MANIFEST.json](PUBLISH_MANIFEST.json).
No custom repository secret is required. The workflow token has `contents: read`.
Actions are pinned to exact official commits; checkout does not retain credentials.

## What Actions check

1. `harness/spec`: run explicit structural lint (closure evidence NOT_CHECKED), then default full `python -m harness lint` against the public fixture. Missing historical closure evidence still fails the full audit.
2. `harness/selftest`: `python -m unittest discover -s tests -v` on Ubuntu/Python 3.11.
3. `harness/merge`: after both pass, run the native `harness ci-context` adapter and verify the checkout SHA matches the event;
   for PRs, check the GitHub merge candidate has exactly the expected base/head parents;
   for main pushes, check the previous main is an ancestor. Check the exact published file list.
   Upload the resulting checkout proof as an Actions artifact.

GitHub uses the PR merge branch for a `pull_request` checkout; see
[official event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request).
This workflow does not merge or deploy by itself. Actual merge requires the project owner's decision.
Required checks or branch protection are separate repository settings; this package does not silently change them.

## Run locally

```sh
python -m harness lint
python -m unittest discover -s tests -v
```

The task named CI-001 is synthetic and stays ready; its author/reviewer labels do not claim a real approval.
The harness gate tests exercise positive and negative readiness, merge and close cases using isolated Git fixtures.

## Evidence and full gates

`python -m harness lint` retains full historical evidence audit. Structural lint never approves a task.
Use `harness evidence pack --report .harness/reports/close.json --output .harness/reports/bundle.json`
and restore an authorized bundle with `harness evidence unpack --bundle .harness/reports/bundle.json --sha256 DIGEST`
before full audit or gate execution. Packing/restoring is not approval and does not publish a report.

`python -m harness --github-actions run ...` and `gate ...` use explicit native event identity.
V1 local evidence remains readable; V2 CI evidence separates source/base/tested commit roles from run ID/attempt.
Core merge/close positive and negative tests cover detached PR and target push contexts, including synthetic
review/owner records, actual Git merges, old evidence, missing approvals, and bundle tampering.
Those synthetic tests are not real project approvals. Actual project gates still require actual independent
review and owner decision evidence from an authorized private transport. A green fixture workflow does not supply them.
