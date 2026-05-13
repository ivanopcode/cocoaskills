# CocoaSkill

`csk` is a local skill manager for already cloned git repositories containing
agent skills.

The MVP design is frozen in [docs/mvp-design.md](docs/mvp-design.md).

## Development

```bash
python -m pip install -e .
python -m pytest
```

The runtime package is stdlib-only. Tests use `pytest`.

