# Contributing to LABRAT

Thank you for helping. LABRAT runs real equipment. A change must not make it easier to damage anything, and
it must be clear enough for the next person to trust.

## Ways to help

- **Report a bug** with the issue template: what you ran, what you expected and what happened.
- **Add a driver** or an `scpi` description of an instrument you use ([docs/drivers.md](docs/drivers.md)).
- **Write a client** for a language or tool you know.
- **Improve the docs** wherever something confused you.

For anything larger than a small fix, open an issue or a discussion first, so we can agree on the approach
before you spend time on it.

## Set up

```bash
git clone https://github.com/reinhardjvv/labrat && cd labrat
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pre-commit install            # optional, runs the checks on every commit
```

## Before you open a pull request

```bash
.venv/bin/ruff check src tests tools
.venv/bin/pytest
.venv/bin/python tools/check_repo.py
```

All three must pass. CI runs them on Python 3.11, 3.12 and 3.13.

## House rules

1. **Safety first.** Never loosen a limit, skip a check or make a safe state optional. A driver copies absolute
   maximum ratings exactly from the manufacturer's documentation and cites the document in a comment.
2. **Tests without hardware.** Every driver and feature comes with tests that run on simulated or fake
   devices, so CI can run them.
3. **Nothing private.** No real IP addresses (use 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24 in
   examples), host names, serial numbers, user names, home paths, tokens or keys. `tools/check_repo.py`
   checks for these.
4. **Plain writing.** Short sentences, concrete facts, UK spelling. Say what a thing does and what it measures,
   with numbers where there are numbers. No marketing words.
5. **Small pull requests.** One change per pull request, with a description of what and why.
6. **Commit messages** in the imperative ("Add a Modbus TCP driver"), with a body explaining why when it is
   not obvious.

## Using AI tools

AI assistants are welcome as tools. You remain the author: read and understand every line you submit, run
the tests yourself, and make sure no private data from your own lab ends up in the change.
[AGENTS.md](AGENTS.md) gives coding agents the rules of this repository.

## Review

A maintainer reviews every pull request. Pull requests from branches of this repository also get an automated
review, which is advice: a maintainer decides. Expect questions about safety, tests and naming.

## Device profiles

A profile for a model LABRAT does not know is one of the most useful contributions. See
[docs/catalog.md](docs/catalog.md#sharing-a-profile): include recordings from a real unit and say where they
came from.

## Licence

By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), the
licence of the project. You keep your copyright. Sign off your commits (`git commit -s`) to certify the
[Developer Certificate of Origin](https://developercertificate.org).
