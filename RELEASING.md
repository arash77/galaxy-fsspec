# Releasing galaxy-fsspec

Releases are published to PyPI by GitHub Actions using [Trusted
Publishing](https://docs.pypi.org/trusted-publishers/). There is no PyPI API
token anywhere in this repository, and no maintainer needs one.

## One-time setup

These steps are done once, by a repository maintainer, before the first release.

1. **Create the `pypi` environment.** Settings, Environments, New environment,
   named exactly `pypi`. Add at least one required reviewer. Do not put any
   secrets in it.
2. **Protect the release tags.** Add a ruleset covering the `v*` tag pattern
   that restricts who may create, update or delete them. A release tag that can
   be moved after the fact defeats the point of checking it.
3. **Register the Trusted Publisher.** While the project does not yet exist on
   PyPI, create a *pending* publisher at
   <https://pypi.org/manage/account/publishing/> with exactly these values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `galaxy-fsspec` |
   | Owner | `bgruening` |
   | Repository name | `galaxy-fsspec` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve the name, so check that
   `galaxy-fsspec` is still free shortly before the first release.
4. **Optional: enable the live Galaxy tests.** Set the repository variable
   `RUN_LIVE_INTEGRATION_TESTS` to `true` and add a `GALAXY_USER_API_KEY`
   secret for a throwaway account. Set `GALAXY_URL` as a variable to test
   against something other than usegalaxy.org. Leaving the variable unset skips
   the live job; setting it without the secret fails the job on purpose, rather
   than skipping quietly.

## Cutting a release

1. Open a pull request that sets `version` in `pyproject.toml` to the new
   version, then run `uv lock` and commit the updated `uv.lock` alongside it.
   `uv.lock` records the root project's own version, so a bump without it
   fails `uv sync --locked` in CI and again in the release build. Nothing else
   needs editing: `__version__` is read from the installed distribution
   metadata.
2. Merge it once CI is green.
3. Tag that exact commit `vX.Y.Z` and push the tag.
4. Publish a GitHub release for the tag. Leave "set as a pre-release"
   unchecked; the publish workflow deliberately ignores prereleases.

   Publishing the release, not pushing the tag, is what starts the workflow.
5. The `build` job re-checks everything from the tagged commit: that the tag
   matches the declared version and points at the commit being built, then
   ruff, mypy, the unit tests, the build, `twine check`, and an install of both
   the wheel and the source archive in throwaway environments.
6. Approve the `pypi` deployment once that job is green. Read the log first:
   the artifact names and the version it reports are printed there, and this is
   the last point at which anything can be stopped.
7. Check the published project page, and install it somewhere clean:

   ```bash
   uv run --isolated --no-project --with galaxy-fsspec==X.Y.Z \
     python -c "import galaxy_fsspec, fsspec; print(galaxy_fsspec.__version__)"
   ```

## If something goes wrong

Files on PyPI cannot be replaced, and a version number cannot be reused even
after the file is deleted. If a bad artifact is published, yank the release on
PyPI and publish a fixed version. Do not move the git tag: the publish workflow
checks that the tag points at the commit it built, so a moved tag will fail the
next time anyone looks.
