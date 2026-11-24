# Security

DiffProwl is a private Quality-Max project. Report suspected vulnerabilities
privately to the maintainers, not in issues, pull requests or chat: use a
private GitHub security advisory on this repository.

## Scope and trust model

- DiffProwl executes the test suites it is pointed at. Repository code, scope
  files and evidence are trusted input; DiffProwl is not a sandbox.
- Baselines and receipts are trusted CI evidence, not signed attestations.
  Content hashes detect change; they do not authenticate an author.
- Release binaries are built and uploaded only by `.github/workflows/release.yml`.
  Pin both the release tag and the SHA-256 of your platform asset, and verify
  the asset was uploaded by `github-actions[bot]`. Never pin a moving ref.
- Never put credentials in scope files, test IDs, receipts or manifests.
  Receipts deliberately omit captured output and exception text.
