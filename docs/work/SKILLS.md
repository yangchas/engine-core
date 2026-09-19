# Project Skill Registry

These skills are provided by the installed native Codex plugin `ecc@ecc`
(version `2.2.1`). The project intentionally does not copy the full ECC skill
tree into the repository.

| Skill | Use in engine_core |
|---|---|
| `contract-first` | Freeze source, unit, time, availability, and missing-value contracts before implementation. |
| `python-testing` | Add focused pytest fixtures and regressions using the server Python 3.12.3 runtime. |
| `verification-loop` | Run tests, compileall, diff checks, and deterministic evidence checks before handoff. |
| `production-audit` | Review read-only boundaries, service state, provenance, and side-effect counters. |
| `team-agent-orchestration` | Maintain ownership, worktree isolation, Kanban state, handoffs, and merge gates. |
| `architecture-decision-records` | Record durable migration decisions and unresolved semantic alternatives. |

Project-specific additions to every skill use:

- `Missing != Zero`.
- `historical oracle != runtime input`.
- `deterministic replay != production batch equivalence`.
- `Fact != Strategy`.
- `read-only != production equivalence`.
- No Rabbit ACK ownership, producer edits, Redis/TD writes, systemd changes,
  retention changes, notifications, orders, or other effects.

If a future read-only discovery check proves a native skill is unavailable,
add only a minimal local wrapper for that named skill; do not vendor the ECC
skill implementation.
