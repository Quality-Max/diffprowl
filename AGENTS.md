# Panthera

LLM-free Rust engine with an optional Python shadow adapter. Keep application-specific
scopes in their application repository. 9lives integration is planned, not implemented.

- Use feature branches. Never commit or push directly to master.
- Do not enable test skipping: shadow comparisons always execute the full scoped suite.
- Keep fingerprints, schema compatibility and failure classification conservative.
- Treat source and evidence as trusted local input, not signed release attestations.
- Never log or commit secrets. Use only non-sensitive test flags in scope files.
- Run the README development checks before committing. Review connected callers and tests.
- Report measured scope and limitations; test-count reduction is not CPU savings.
