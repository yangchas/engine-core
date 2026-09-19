# engine_core Codex/ECC work area

This directory is the project-local control pane for bounded Core work. The
native `ecc@ecc` plugin supplies shared skills; this repository adds only the
project-specific role contracts, task board, plans, and handoffs.

## Operating rules

- One owner per task card and one isolated worktree for write-capable work.
- Do not run multiple writers against the same file or worktree.
- `READY` is not execution approval; a task must be explicitly invoked.
- Every handoff records evidence, unknowns, side-effect counters, and the next owner.
- No autonomous loop, `/loop`, multi-execute, scheduled task, or MCP server is enabled.
- Use explicit sandbox flags for every Codex invocation.

Read-only example:

```bash
codex exec --ephemeral --sandbox read-only -C /home/exedev/repos/engine_core \
  "Use the planner role for TASK-001; do not edit files or run replay."
```

The project-local config defaults to `workspace-write` for approved local
changes, with network access disabled and `/home/exedev/validation` as the
only additional writable root.
