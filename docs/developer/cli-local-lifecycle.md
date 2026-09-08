# CLI local lifecycle

The CLI owns local workspace and key writes through the one-shot bootstrap
container. The public API exposes read-only identity and admin listings only.

This keeps SaaS workspace lifecycle out of the core engine. Remote agents use
`INHERENT_URL` and `INHERENT_API_KEY` for reads; `keys create` and `keys revoke`
require the local stack started by `inherent up`.
