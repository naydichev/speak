# Development

```sh
git clone https://github.com/naydichev/speak && cd speak
uv sync
uv run speak                     # run it without installing
```

To make your checkout *be* the installed `speak`, so edits apply on next
launch:

```sh
uv tool install --editable .
```

Editable tracks source *files*, not metadata, so after bumping the version the
installed copy still reports the old one until you reinstall:

```sh
uv tool install --editable . --force
```

## Layout

```
src/speak/core.py         speech, config, voice parsing, fuzzy matching — no UI import
src/speak/app.py          the textual app
tests/test_core.py        headless
tests/test_app.py         driven through textual's pilot
tests/manual_exits.py     needs a real pty and signals; run by hand
docs/screenshots.py       regenerates the images in the README
```

## Tests

`core.py` imports no UI, so the logic is testable without a terminal:

```sh
uv run pytest
```

One check needs a real pty and real signals, so it is not a pytest:

```sh
python3 tests/manual_exits.py
```

It ends the app six ways (`^C`, `^D`, `^Q`, SIGTERM, SIGHUP, SIGINT) and asserts
each turns mouse tracking back off. If any does not, the terminal keeps
reporting mouse motion to a shell that echoes it, and the screen fills with
fragments like `M35;2262;-3M` over the dead app's last frame — which reads as
the app corrupting itself. SIGHUP is the one to watch: it is what a closing
terminal window sends.

## Screenshots

Generated, not pasted, so they cannot drift from the interface:

```sh
uv run python docs/screenshots.py
```

## Releasing

CI runs the suite on `macos-latest` for every push and pull request.

To release: bump `version` in `pyproject.toml`, then tag it.

```sh
git tag v0.2.0 && git push --tags
```

That publishes to PyPI through a trusted publisher, so there is no API token
anywhere. The job refuses to build if the tag and the project version
disagree — PyPI will not accept a re-upload of a version, so a mismatch would
be unrecoverable.
