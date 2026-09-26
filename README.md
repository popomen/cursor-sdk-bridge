# cursor-bridge

A local Cursor SDK adapter exposing OpenAI Responses and Anthropic Messages, with Codex / Claude Code switching and a private metadata dashboard.

The initial migration preserves the behavior of `codex-cursor-fallback`; the reliability and native-tool engine are being introduced in independently reversible steps. Runtime state remains in its existing directories. `docs/archive/` contains historical incident and experiment records; commands there refer to the previous skill layout.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.codex/cursor-sdk2api/venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

No test requires inference or production mutation.
