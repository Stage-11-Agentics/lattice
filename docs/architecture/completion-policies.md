# Completion Policies

## Purpose

Completion policies gate status transitions based on evidence and assignment,
primarily to enforce review discipline before `done`.

Policy logic lives in `src/lattice/core/config.py` via
`validate_completion_policy()`.

## Config Shape

Location: `config.workflow.completion_policies`

Policy keys are target statuses. Supported policy fields:

- `require_roles`: list of required evidence roles
- `require_assigned`: bool (task must have `assigned_to`)

Example:

```json
{
  "workflow": {
    "completion_policies": {
      "done": {"require_roles": ["review"], "require_assigned": true}
    }
  }
}
```

## Evaluation Semantics

- Policies are evaluated on transition into `to_status`
- Missing policy for a status means no policy gate
- `universal_targets` (default: `needs_human`, `cancelled`) bypass policies

Role checks use `get_evidence_roles(snapshot)` from `core/tasks.py`, which reads
`evidence_refs` (with legacy fallback support).

## How Roles Are Satisfied

Roles enter `evidence_refs` through:

- `lattice comment --role <role>`
- `lattice attach --role <role>`
- equivalent dashboard/API write flows

Role presence, not comment text quality, satisfies policy mechanically.

## CLI Integration

`lattice status` evaluates policies before writing `status_changed` events.

Failure behavior:

- without `--force`: transition blocked with `COMPLETION_BLOCKED`
- with `--force`: requires `--reason`

Review-cycle limits are separate but related gates:

- from `review` to rework states (`in_progress`, `in_planning`)
- limit defaults to `3` (`workflow.review_cycle_limit`)

## Role Vocabulary

Valid configured roles are resolved by `get_configured_roles()` from:

- `workflow.roles`
- any `require_roles` in completion policies

Default config includes `review`.

## Troubleshooting

If a transition to `done` fails unexpectedly:

1. Check policy config in `.lattice/config.json`
2. Inspect snapshot `evidence_refs` for required roles
3. Confirm assignment if `require_assigned` is enabled
4. Retry with proper role evidence (preferred) over force
