# Protocol guide

The static guide explains the Python Cursor SDK bridge at source commit
`abcca9367a3fe918466f0a8e316c16003bcd1575`. Open `index.html`, or serve this directory
over HTTP. The browser uses no external libraries, fonts, analytics, API calls, or
model inference. Its examples contain synthetic data.

The guide includes English and Chinese views, an interactive tool cycle, field
mappings, SSE sequences, recovery behavior, compatibility limits, and source
references. English prose applies ASD-STE100 Issue 9 writing principles. Full
controlled-dictionary compliance requires specialist review; the page does not
claim certification. Code identifiers keep their literal spelling.

## Local preview

From the repository root:

```sh
python3 -m http.server 8894 --bind 127.0.0.1 --directory docs/protocol-guide
```

Then open `http://127.0.0.1:8894/`. The demo never contacts the bridge API.

## Installed HTTP service

The installed user unit is `cursor-sdk-bridge-guide.service`, on port `8794`.
Its document root contains only `index.html`, `style.css`, and `app.js` from a
committed version. It does not serve the repository or runtime state directories.

Releases are under `~/.local/share/cursor-sdk-bridge-guide/releases/<commit>/`.
The unit points directly to its release, so checkout edits do not affect the page
being served. It uses Python's static HTTP server, with IPv4/IPv6 access on the
host network. This is a documentation service, not a bridge API endpoint.

```sh
systemctl --user status cursor-sdk-bridge-guide.service
curl --fail http://127.0.0.1:8794/ -o /dev/null
```

To update the page, validate and commit the static files, install their committed
contents into a new release directory, and change only the guide unit's document
root. Restart only this documentation unit. Do not deploy or restart the Codex,
Claude Code, TraeX, or dashboard services for a documentation change.

For access through an existing SSH connection, forward local port `8794` to
`127.0.0.1:8794` on the host. Direct access requires network reachability to the
host. The service contains documentation only and does not provide authentication.

## Validation

Run the repository's deterministic tests as documented in `AGENTS.md`. Validate
the page at desktop and phone widths, exercise both protocol paths and all six
steps, and check language selection, copy, playback, print, local anchors, and
the absence of external requests. These checks do not need a real SDK call.

Keep sanitized validation and deployment receipts in `docs/evidence/`.
