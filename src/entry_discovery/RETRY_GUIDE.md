# Single-invocation discovery and recovery

From the `entry_discovery` directory:

```powershell
python -u .\src\run_entry_discovery.py --revision-batch-size 50 --delay 3.5 --cooldown 120 --max-cooldown 1800 --max-delay 30 --max-consecutive-429 0 --recovery-rounds 5
```

Omit `--batch-size` to reuse the existing checkpoint's batch size (50 for a fresh output). Do not delete checkpoints or the saved cooldown to try to speed up the crawl. An existing cooldown deadline, including an old 600-second deadline, is honored before the next request. For new 429 responses, the new policy applies. Do not run two copies against the same checkpoint directory.

| Change | Behavior and purpose |
| --- | --- |
| Automatic 429 recovery | Default `--max-consecutive-429 0` keeps waiting and retrying the same task in the same process; it no longer exits after two 429 responses. A positive value remains available as an explicit stop limit. |
| Fallback exponential backoff | With absent/invalid `Retry-After`, waits 120, 240, 480, 960, then 1,800 seconds; repeated failures stay at 1,800. `--cooldown` sets the initial fallback; `--max-cooldown` caps only this fallback. A successful fetch resets the consecutive 429 count. |
| Server-directed wait | A valid `Retry-After` is honored, with the configured base delay as a minimum. There is no forced 600-second floor. Server-directed waits can exceed `--max-cooldown`. |
| Adaptive pacing | After a 429, delay increases by 50% (at least 5 seconds, subject to the configured cap). After 20 consecutive successful fetches, delay decreases by 10%, never below `--delay`. Requests remain sequential; base delay must be at least 3 seconds. |
| Missing-task recovery | Metadata and revision caches are revisited automatically. Only IDs/versions still missing are requested; previously saved successful results are reused. Transient network failures, malformed XML and incomplete responses can recover without restarting the command. |
| Bounded stalled rounds | After five consecutive recovery rounds without any new metadata or revision date, stops with `stopped_reason=no_progress_limit`, preserving pending tasks and diagnostics. Any progress resets the stalled-round counter. Change using `--recovery-rounds`. |
| Permanent HTTP errors | Non-429 HTTP 4xx errors stop with `permanent_http_error`; repeated automatic requests cannot fix errors such as HTTP 403. HTTP 5xx/network failures retain the existing client retries and may also recover in later rounds. |
| Live progress | Logs show round, request, task, batch size, current delay, backoff reason and countdown. A per-round summary shows completed/pending papers. |
| Persistent state | Successful batches/revision dates are saved as before. Cooldown files also save consecutive 429 count and current delay; old deadline-only files remain readable. Errors and attempt counters are written on request failures and each recovery round. Ctrl+C still writes the final summary and queue. |

“One invocation” means automatic waiting and recovery without manually rerunning the command for transient failures. It cannot guarantee completion if the service never becomes available, access is refused, or requested data remains missing. Repeated 429s wait indefinitely by default; Ctrl+C stops with checkpoints preserved. For other persistent failures the limits above prevent silent infinite loops. The handoff still runs only after complete validation.

`--pass-number` remains a report label; it does not control recovery rounds. `--max-requests` and `--no-wait-cooldown` remain available when running `smart_discovery.py` directly for diagnostics.

The client preserves the [arXiv API requirements](https://info.arxiv.org/help/api/tou.html) of one request per three seconds at most and one connection at a time. Reduced wall time depends on actual server responses; no live performance improvement is claimed from simulated tests.

## Changed files

- `src/smart_discovery.py`: backoff, adaptive pacing, pending-task recovery, persistent cooldown state and logs.
- `src/discovery.py`: distinguishes non-retryable HTTP errors using `PermanentRequestError`, a `RuntimeError` subclass compatible with existing handlers.
- `src/run_entry_discovery.py`: exposes and forwards the new options; initial fallback default changes from 600 to 120 seconds.
- `tests/test_smart_recovery.py`: offline integration tests using fake responses and a virtual clock; never contacts arXiv or sleeps in real time.

Run the targeted tests from `entry_discovery`:

```powershell
python -m unittest discover -s tests -p test_smart_recovery.py -v
```

The existing full suite also contains two unrelated failures: `test_discovery.py` imports the removed `parse_atom` API, and `test_group.py` assumes the current team configuration has missing member ranges. These tests were left unchanged.
