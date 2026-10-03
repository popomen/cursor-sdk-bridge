# TraeX protocol core

Source: https://code.byted.org/hepengcheng/traex_bridge

Version: 0.6.4
Commit: 9f6535f4940cf957b08eb4bb266fef10cef84f93

The source and synthetic tests are vendored at this commit. Local integration lives in bridge-*.ts and the optional BridgeRuntime hooks in app.ts and explicit authentication requirement in api-key.ts. The managed entry point only exposes Astra Responses and metadata/admin endpoints. Upstream server.ts is not used by deployment.

Build with `npm ci && npm run build:bridge`; verify with `npm test && npm run typecheck`. Generated bundles and node_modules are not tracked. Never copy credentials, runtime data, or debug dumps into this directory.

Upstream Docker/ICM publishing tests are omitted because releases are built and installed by the parent Python project. Protocol, credential, admission and quota tests are retained.
