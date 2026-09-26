# Cursor Bridge

Work on main in this checkout. Do not touch production services unless the task authorizes it; confirm progress, queued requests, and open connections are idle immediately before any restart. Never stop the Codex daemon from its own active task.

Preserve existing state directories and credentials. Never print or commit credentials, request bodies, tool results, or private model output. Runtime request logs contain metadata only. Run deterministic tests with:

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.codex/cursor-sdk2api/venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Real SDK calls consume the user's Cursor quota: announce a rough cost and use them only for explicit experiments, preflight, or acceptance. Keep sanitized receipts under docs/evidence. Deployment installs committed code into versioned directories; editing the checkout must never change running services. Coordinate file ownership when using agents; root stages and commits only after review.
