// Load the same ESM SDK instance as the pinned bridge, before its entrypoint.
import { pathToFileURL } from 'node:url';

const sdk = new URL('../../node_modules/@cursor/sdk/dist/esm/index.js', pathToFileURL(process.argv[1]));
const { Cursor } = await import(sdk.href);
Cursor.configure({ local: { useHttp1ForAgent: true } });
