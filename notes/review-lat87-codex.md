# Code Review: PR #1 — LAT-87 (Transition-specific hooks)

## Correctness

1. **[Medium] `* -> *` is accepted by parser but never matches at runtime.**
   - **Where:** `src/lattice/storage/hooks.py:97`, `src/lattice/storage/hooks.py:87`
   - `_parse_transition_key()` accepts `"* -> *"` and returns `("*", "*")`, but `_match_transitions()` has no branch that handles both wildcards.
   - Result: config that appears valid silently does nothing for this pattern.
   - Suggested fix: either (a) support `* -> *` explicitly in `_match_transitions()`, or (b) reject it in `_parse_transition_key()` and document it as unsupported.

2. **[Medium] Documented wildcard precedence is not implemented deterministically.**
   - **Where:** `src/lattice/storage/hooks.py:73`, `src/lattice/storage/hooks.py:80`
   - Docstring says priority is exact, then wildcard source (`* -> to`), then wildcard target (`from -> *`).
   - Implementation appends wildcard matches in dict iteration order, so `from -> *` may run before `* -> to` depending on config key order.
   - Suggested fix: collect into separate buckets (`exact`, `wild_src`, `wild_tgt`) and return `exact + wild_src + wild_tgt`.

3. **[Low/Medium] Fire-and-forget contract can be broken by malformed `hooks.transitions`.**
   - **Where:** `src/lattice/storage/hooks.py:22`, `src/lattice/storage/hooks.py:80`
   - `execute_hooks()` promises hook failures do not raise, but `_match_transitions()` assumes `transitions` is a dict and calls `.items()` unguarded.
   - If config contains a wrong type (e.g. string/list), this raises `AttributeError` and can fail the caller.
   - Suggested fix: defensive type-check (`isinstance(transitions, dict)`) before matching; otherwise log and skip.

## Design

1. Hook layering (`post_event` -> `on.<type>` -> `transitions`) is a good extension and matches existing mental model.
2. New env vars (`LATTICE_FROM_STATUS`, `LATTICE_TO_STATUS`) are useful and scoped correctly to transition hooks.
3. **Open question:** `validate_transition()` now returns `True` for universal targets before checking `from_status` membership (`src/lattice/core/config.py:177`). If this is intentional, it should be documented explicitly as allowing recovery from unknown current statuses.

## Security

1. No new critical security issue beyond existing hook model (`shell=True` command execution from local config).
2. Transition hook env vars carry status text into subprocess env; this is expected, but scripts should treat env values as untrusted text (especially in shell interpolation).

## Test Coverage

1. Coverage is broad and valuable (unit + CLI integration), but key gaps remain:
   - Missing test that `* -> *` actually matches a `status_changed` event.
   - Missing test that wildcard-source runs before wildcard-target regardless of config key order.
   - Missing robustness test for malformed `hooks.transitions` type to enforce non-raising behavior.
2. Existing parser test for `* -> *` (`tests/test_storage/test_hooks.py:399`) increases expectation that it should be supported, but matcher tests do not validate this behavior.

## Code Quality

1. Good factoring with `_parse_transition_key()` and `_match_transitions()` keeps `execute_hooks()` readable.
2. Naming and style are consistent with current module conventions.
3. Main quality issue is semantic mismatch between docstrings/tests and actual matcher behavior.

## Summary Verdict

**Request changes**.

The PR is close and generally well-structured, but the matcher semantics need tightening before merge: fix `* -> *` handling (or reject it), enforce documented precedence deterministically, and harden malformed-config behavior to preserve fire-and-forget guarantees.
