# Releasing

For maintainers.

1. Make sure `main` is green in CI.
2. Set `__version__` in `src/labrat/__init__.py`, following [semantic versioning](https://semver.org).
3. Move the `Unreleased` notes in `CHANGELOG.md` under the new version with today's date.
4. Commit as `Release vX.Y.Z`, tag `vX.Y.Z`, and push the tag.

The `release` workflow then runs the tests, builds the wheel and source archive, publishes them to PyPI with
trusted publishing, and makes a GitHub release with the changelog's notes and the files attached.

## One-time set-up

- On PyPI, add a trusted publisher for the project `labrat-lab`: owner `reinhardjvv`, repository `labrat`,
  workflow `release.yml`, environment `pypi`.
- On GitHub, create the environment `pypi`, and protect it so only tags matching `v*` can deploy.
- For automated review, add the repository secret `ANTHROPIC_API_KEY`. Without it the review job is skipped.
