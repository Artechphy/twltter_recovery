# Public CI package

This repository contains a generic harness runtime and explicitly synthetic CI metadata.
Keep the public file list in PUBLISH_MANIFEST.json accurate. Do not add private project
decisions, historical reports, credentials, or machine-specific snapshots.
Run `python -m harness lint` and `python -m unittest discover -s tests -v`.
Actions validate the checked-out merge candidate; they do not authorize a merge or deployment.
