# ADR-0073: The GitHub throttle waits on the reported budget

- Status: Accepted
- Date: 2026-11-24
- Tracks: #3734
- Refines: ADR-0071

## Context

The global GitHub throttle uses a token bucket in a node-local JSON file
(ADR-0071). Every `gh` call spends one token. The default refill rate is about
0.69 calls per second. An operator can select a lower rate.

A low rate paces a call even when the account has budget left. The automation
loop used `--gh-global-rate 0.2` as a workaround. That workaround added a 5 s
wait to each call and a repeated debug line to the log.

GitHub reports the remaining budget and the reset time for each API resource.
The `gh api rate_limit` endpoint returns this data. A probe of this endpoint
does not spend the account budget.

## Decision

1. The global throttle reads the GraphQL budget from one cached
   `gh api rate_limit` probe. The cache lifetime is 30 s for each process. One
   cache entry holds the `(remaining, reset)` pair from the same probe, so a
   caller does not mix values from two windows.
2. A budget above 100 remaining points returns immediately. The call does not
   spend a token from the bucket.
3. A budget at or below 100 remaining points waits until the reported reset
   plus 5 s. Then the call returns. A later call probes the new window.
4. An unknown budget uses the existing token bucket. The probe can fail, and
   the throttle must still work. The configured `--gh-global-rate` and
   `--gh-global-burst` values apply to this fallback.
5. `--gh-global-rate 0` disables the budget wait and the bucket. The throttle
   does not probe GitHub in this mode.
6. The throttle reports the resolved state root one time for each process.
7. The node-local scope, the private state directory, the lock, the deadline,
   and the cancellation behavior do not change (ADR-0071). The probe obeys the
   operation deadline and the cancellation event. One probe is in flight at a
   time, and the throttle re-checks the deadline before it returns.
8. The throttle accepts a reported reset only when it is within one window
   (65 minutes). A reset further away can come from a corrupted payload, a
   clock error, or a value in the wrong unit. The throttle logs a warning and
   uses the token bucket for that call.

## Alternatives considered

- Keep the token bucket for every call. A low rate gives a long wait while the
  account has budget left. This alternative does not use the reported reset.
- Use only the budget and remove the bucket. A failed probe then removes all
  pacing. The fallback keeps a safe path for an unknown budget.
- Wait on the REST core budget. The automation loop spends GraphQL points. The
  REST and GraphQL budgets are separate.
- Set the bucket rate from the remaining budget. This design adds a second
  control loop. The reset wait gives the same result with less state.

## Consequences

- A healthy budget removes the fixed pace from the call path. An operator does
  not need a low `--gh-global-rate` workaround.
- A healthy budget also skips the token bucket, so requests above the reserve
  are not serialized across processes. A burst of worker processes can spend
  points at the same time. The reported budget, refreshed every 30 s, is the
  control for that case. Each process still sees its own cached view.
- A depleted GraphQL budget stops the caller until the window resets. A REST
  caller also stops, because one throttle serves the account. This behavior is
  conservative. The account is near a limit.
- The probe adds one `gh` subprocess call for each 30 s in each process. The
  probe does not spend the account budget.
- The account budget is global. The reserve and the cache are per host. A
  second host can spend the budget between probes, and this process cannot see
  that spend until the next probe.
- A test must replace the budget probe when it checks the token bucket. The
  shared test fixture does this and points the throttle at a temporary root.

## Rollback

Revert the change. The token bucket again paces every call. The probe cache and
the budget wait are removed. No persisted state changes.
