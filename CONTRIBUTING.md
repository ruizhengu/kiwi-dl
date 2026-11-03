# Contributing

```bash
git clone https://github.com/ruizhengu/kiwi-dl.git
cd kiwi-dl
uv venv && uv pip install -e ".[dev]"
pre-commit install
```

Before opening a pull request:

```bash
.venv/bin/ruff check src tests
.venv/bin/black --check src tests
.venv/bin/mypy src
.venv/bin/pytest
```

Keep changes focused and add a test for changed behaviour. Tests must not touch
the network or need a MEGA account: use `tests/fake_mega.py`, which serves really
encrypted files and answers the way MEGA does.

## Layout

```text
src/kiwi_dl/
  cli.py        `mg-dl` and `kiwi-dl`
  commands/     download, info, proxy, config
  core/         links, crypto, API client, downloader, resume state
  proxy/        proxy pool and the SmartProxy chunk router
  ui/           progress and prompts
  utils/        logging, redaction, locks, hooks
tests/
```

Never put a link key, a proxy password or a session id in a log line, an error
message or a test fixture that gets printed.
