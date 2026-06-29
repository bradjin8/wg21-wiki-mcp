# First PyPI publish (one-time setup)

The [publish workflow](../.github/workflows/publish.yml) runs automatically on
`v*` tag push after this one-time configuration. Until the first successful
publish, `pip install wg21-wiki-mcp` will not resolve on PyPI even though the
workflow and README install instructions are in place.

## Prerequisites

- Merge rights on `cppalliance/wg21-wiki-mcp`
- PyPI account with permission to create or claim the `wg21-wiki-mcp` project
- GitHub admin on the repository (for environments and rulesets)

## 1. Configure Trusted Publishing on PyPI

1. Open [pypi.org/manage/project/wg21-wiki-mcp/settings/publishing/](https://pypi.org/manage/project/wg21-wiki-mcp/settings/publishing/)
   (create the project name first if it does not exist).
2. Add a **pending** trusted publisher:
   - **PyPI project name:** `wg21-wiki-mcp`
   - **Owner:** `cppalliance`
   - **Repository:** `wg21-wiki-mcp`
   - **Workflow name:** `Publish` (file `publish.yml`)
   - **Environment name:** `pypi`

## 2. Create the GitHub `pypi` environment

1. Repository **Settings → Environments → New environment** → name `pypi`.
2. Optionally restrict deployment branches to `master` (tags are evaluated against
   the commit; matching org policy is sufficient).
3. No long-lived PyPI password is required — OIDC supplies the token at publish time.

## 3. Cut a release tag that matches package version

The publish job **fails** if the tag does not match `version` in
`pyproject.toml` and `__version__` in `src/wg21_wiki_mcp/__init__.py`.

Follow [CONTRIBUTING.md](../CONTRIBUTING.md#branching-and-releases):

1. Bump version on `develop`, update `CHANGELOG.md`.
2. PR `develop` → `master`; merge when CI is green.
3. Tag the merge commit on `master` and push:
   `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`

Use a tag created **after** `publish.yml` merged (e.g. `v0.2.1` if `v0.2.0`
predates the workflow).

## 4. Verify the publish workflow

On tag push, the **Publish** workflow should:

1. Build sdist + wheel
2. Generate CycloneDX SBOM and Sigstore bundles
3. Upload to PyPI via Trusted Publisher
4. Attach artifacts to the GitHub Release

Confirm:

```bash
pip index versions wg21-wiki-mcp
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Workflow skipped | Tag must match `v*`; workflow triggers on tag push only |
| Trusted publisher rejected | Owner/repo/workflow/environment must match PyPI settings exactly |
| Version mismatch error | Tag `vX.Y.Z` must equal `pyproject.toml` and `__init__.py` |
| Environment missing | Create GitHub environment `pypi` |
