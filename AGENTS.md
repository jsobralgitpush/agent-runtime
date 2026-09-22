# Agent Runtime contributor guide

## Objective

Keep this repository an understandable, production-minded reference implementation of an AI workflow runtime. Prefer explicit behavior and small interfaces over framework magic.

## Required checks

Before considering a code change complete, run:

```bash
make check
```

For changes to migrations or the execution path, also run the end-to-end demo against a running API:

```bash
make demo
```

## Architecture boundaries

- Keep HTTP concerns in `app/api.py`.
- Keep workflow semantics in `app/engine.py`; the engine must remain callable outside FastAPI.
- Provider-specific response formats belong behind `LLMProvider`.
- Tools must be explicitly registered. Do not dynamically import code from workflow definitions.
- Preserve database-enforced idempotency and bounded retry/timeout validation.
- Never persist or log provider credentials.

## Change expectations

- Add tests for behavior and failure paths.
- Update architecture docs or create an ADR for meaningful trade-offs.
- Do not claim queue durability, multi-tenancy, or production readiness until implemented and verified.
- Use focused commits with conventional prefixes such as `feat:`, `test:`, `docs:`, and `chore:`.
