# GraphQL Server

Rindexer runs PostGraphile 5.2.1 and its Grafserv Express 4 adapter in the `rindexer-graphql` binary. The server exposes `/graphql` and Apollo Sandbox at `/playground` on the same port.

## Development

From the `graphql` directory, pass a PostgreSQL connection URL and the comma-separated list of schemas to expose.

```bash
npm ci
node index.js "$DATABASE_URL" my_indexer_contract 3001 1000 10000
```

## Build

The Rust build runs `npm run build`. esbuild resolves the PostGraphile package exports before pkg embeds the bundle in the executable.

## Requirements

- Node.js 22 or newer
- npm
