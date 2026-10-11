# Evidence storage policy

- Keep concise, reviewable Markdown and small JSON summaries in this directory
  and commit them with the related code or audit.
- Keep large raw captures and replay payloads outside Git, under a dated
  `/home/exedev/validation/` directory. Include a manifest, SHA-256 checksums,
  source/observation time, and a link here or from the related handoff.
- Review evidence by content, sensitivity, provenance, and usefulness. There is
  no directory-wide 1 MiB cap; size alone is not a reason to reject a concise,
  reviewed artifact.
- `*.jsonl`, `*.csv`, and `*.gz` are ignored here as raw-data formats. A small
  intentional fixture in one of these formats may be force-added only after
  review and an explicit reason plus SHA-256 entry in the raw-evidence
  allowlist used by `tests/test_repo_hygiene.py`.
- Never store credentials, tokens, cookies, or authentication files in either
  location.
