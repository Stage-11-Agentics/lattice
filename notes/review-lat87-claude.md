# Code Review: LAT-87 -- Transition-specific hooks for status changes

**Reviewer:** Claude (Opus 4.6)
**Files reviewed:** `src/lattice/core/config.py`, `src/lattice/storage/hooks.py`, `tests/test_storage/test_hooks.py`

---

## Verdict: APPROVE with comments

The PR is well-structured, follows existing codebase patterns faithfully, and ships with thorough tests. The issues identified below range from a real bug (double-wildcard `* -> *` silently dropped) to minor design suggestions. None are blocking.

---

## 1. Correctness

### BUG: `* -> *` (double wildcard) never matches

The `_match_transitions` function has three branches:

```python
if pat_from == from_status and pat_to == to_status:      # exact
elif pat_from == "*" and pat_to == to_status:              # wildcard source
elif pat_from == from_status and pat_to == "*":            # wildcard target
```

A pattern like `"* -> *"` falls through all three conditions because `"*" != from_status` and `"*" != to_status` (assuming actual status names). The parser test `test_both_wildcards` confirms the key *parses* correctly, but there is no matching test for `_match_transitions` that verifies `* -> *` actually matches a concrete transition. The integration test `test_transition_does_not_fire_for_non_status_event` uses `* -> *` only to verify it does NOT fire for non-status events -- it never tests that it DOES fire for a status_changed event.

This means a user who configures `"* -> *": "notify-all-transitions.sh"` will find it silently never fires.

**Fix:** Add a fourth branch:
```python
elif pat_from == "*" and pat_to == "*":
    matched.append(cmd)
```

Or restructure to check wildcard membership:
```python
from_matches = (pat_from == "*" or pat_from == from_status)
to_matches = (pat_to == "*" or pat_to == to_status)
if from_matches and to_matches:
    # insert(0) for exact, append for wildcards
```

**Severity:** Medium. The parser accepts `* -> *`, but the matcher silently ignores it. This is a functional gap that users will hit.

### MINOR: `Workflow` TypedDict changed to `total=False` as a side effect

The diff changes `class Workflow(TypedDict)` to `class Workflow(TypedDict, total=False)` to accommodate the new `universal_targets` field. This is correct for making `universal_targets` optional, but it also makes `statuses`, `transitions`, and `wip_limits` optional when they previously were required. If any code path assumed these keys exist on a `Workflow` without `.get()`, this could mask type errors.

In practice, the runtime code already uses `.get()` defensively everywhere, so this is not a runtime bug. But it weakens the TypedDict contract. Consider either:
- Keeping the required fields as required (use inheritance or `Required[]`)
- Or accepting the tradeoff and documenting that all workflow fields are optional at the type level

**Severity:** Low. No runtime impact, but reduces type safety.

### OK: `validate_transition` universal targets short-circuit

The universal targets logic is correct: if `to_status in universal`, return `True` immediately before checking per-status transitions. This means `done -> cancelled` is now valid (cancelled is universal), which matches the test `test_universal_target_done_to_cancelled`. The behavior is well-tested and intentional.

One subtle consequence: `done` and `cancelled` are listed as universal targets, meaning you can transition from `done -> cancelled` and `cancelled -> cancelled`. Whether self-transitions (`cancelled -> cancelled`) should be allowed is a design question, not a bug, but worth noting.

---

## 2. Design

### Execution order is well-defined and documented

The three-tier execution order (post_event -> on.<type> -> transitions) is clearly documented in the docstring and follows the existing pattern of broadest-first. Good.

### Transition env vars are additive (not replacing)

`transition_env = env.copy()` correctly extends the base env rather than replacing it. Transition hooks get all the standard `LATTICE_*` vars plus `LATTICE_FROM_STATUS` and `LATTICE_TO_STATUS`. Clean design.

### Config shape is reasonable

`hooks.transitions` as `dict[str, str]` (pattern -> command) is the right shape for a JSON config. The arrow syntax `"from -> to"` is human-readable and unambiguous given the parser's behavior.

### Match ordering relies on dict iteration order

The `_match_transitions` function iterates `transitions.items()`, which in Python 3.7+ preserves insertion order. Exact matches get `insert(0)`, wildcards get `append()`. This means if a user has multiple wildcards, their relative order depends on config file key ordering. This is fine for JSON (which preserves key order in Python's `json.loads`), but it is worth documenting that wildcard-to-wildcard ordering follows config file order.

### Multiple exact matches can occur (whitespace normalization)

The test `test_multiple_exact_matches` shows that `"in_progress -> review"` and `"in_progress->review"` both match. Both are `insert(0)`'d, so the last-encountered exact match ends up at position 0. This is a bit surprising -- the user might expect only one to match. However, since dict keys are distinct strings, this only happens with whitespace variations of the same logical pattern. Documenting that keys are whitespace-normalized for matching (but must be distinct JSON keys) would help.

---

## 3. Security

### Shell execution from config (acceptable, existing pattern)

`_run_hook` uses `shell=True` with commands from the config file. This is the same pattern as the existing `post_event` and `on.<type>` hooks. Since config.json is a trusted, local file owned by the project operator, this is acceptable. The hook commands are not sourced from event data or user input.

### Env var values come from event data

`LATTICE_FROM_STATUS` and `LATTICE_TO_STATUS` are populated from `event["data"]["from"]` and `event["data"]["to"]`. These values originate from the CLI's `status` command, which validates statuses against `config.workflow.statuses` before writing the event. So the values are constrained to valid status names (alphanumeric + underscores), not arbitrary user input. No injection risk here.

### Empty status guard

The `if from_status and to_status:` guard at line 58 of hooks.py prevents matching against empty strings, which is a good defensive measure against malformed events.

---

## 4. Test Coverage

### Strengths

- **31 new tests** covering unit, integration, and CLI levels. Thorough.
- Parser tests cover all edge cases: whitespace variations, missing parts, multiple arrows, empty strings.
- Matcher tests cover exact, wildcard source, wildcard target, no match, malformed keys, empty dict.
- Integration tests verify hooks fire through the full CLI -> event -> hook pipeline.
- Negative tests (wrong transition, wrong event type) are included.
- Env var tests confirm the new `LATTICE_FROM_STATUS`/`LATTICE_TO_STATUS` vars are set.
- The `test_all_three_hook_types_fire_together` test is a good interaction test.

### Missing test cases

1. **`* -> *` match test for `_match_transitions`** -- as noted in the bug above, there is no test that `_match_transitions({"* -> *": "cmd.sh"}, "x", "y")` returns `["cmd.sh"]`. The only `* -> *` test is in the non-status-event negative test.

2. **Ordering test between wildcard source and wildcard target** -- `test_exact_before_wildcards` verifies exact is first, but does not assert the relative order of `wildcard-source.sh` vs `wildcard-target.sh`. If ordering between wildcard types matters, it should be tested. If it does not matter, the docstring should say so.

3. **Transition hook with empty `data` dict** -- what happens if a `status_changed` event has `data: {}`? The guard `if from_status and to_status` handles this (both would be empty string, which is falsy), but an explicit test would document the behavior.

4. **Transition hook with missing `data` key entirely** -- `data = event.get("data", {})` handles this, but no test covers it.

5. **Config with `hooks.transitions` set to a non-dict value** -- e.g., `"transitions": "bad"`. The `.get("transitions")` would return a truthy string, then iterating `.items()` would fail. A defensive `isinstance` check or a test documenting the failure mode would help.

6. **Test for `universal_targets` in config** -- the config tests (`test_config.py`) do have `test_has_universal_targets` and the transition validation tests, but there is no test that `validate_transition` with a `done` from-status and a non-universal target (like `in_planning`) returns `False`. The existing `test_terminal_status_has_no_explicit_transitions` covers this partially.

---

## 5. Code Quality

### Naming

All names are consistent with the existing codebase: `_match_transitions`, `_parse_transition_key`, `transition_env`, `LATTICE_FROM_STATUS`. Good.

### Structure

The new code follows the existing hook execution pattern exactly. The `_match_transitions` and `_parse_transition_key` functions are properly separated as private helpers. The test file follows the existing numbered section pattern (sections 9-12).

### Consistency

- The test import block correctly exposes `_match_transitions` and `_parse_transition_key` for unit testing. Testing private functions is appropriate here since they contain non-trivial logic.
- Config TypedDict changes use `total=False` consistently.
- The default config addition (`universal_targets`) is placed logically within the workflow block.

### Minor style note

In `test_all_three_hook_types_fire_together`, the inline script generation loop is compact but slightly less readable than the explicit pattern used in other tests. Not a problem, just a style shift.

---

## Summary of Findings

| # | Finding | Severity | Category |
|---|---------|----------|----------|
| 1 | `* -> *` double wildcard never matches | Medium | Bug |
| 2 | `Workflow` TypedDict made fully optional | Low | Design |
| 3 | No test for `* -> *` matching a status_changed event | Medium | Test gap |
| 4 | Wildcard-to-wildcard relative ordering undocumented/untested | Low | Test gap |
| 5 | No defensive check if `transitions` config value is non-dict | Low | Robustness |
| 6 | Missing edge-case tests for empty/missing event data | Low | Test gap |

---

## Recommendation

**Approve with one required fix:** Address the `* -> *` double-wildcard bug (finding #1). The rest are minor improvements that can be addressed in follow-up work or ignored at the author's discretion. The overall implementation is clean, well-tested, and follows the established patterns of the codebase.
