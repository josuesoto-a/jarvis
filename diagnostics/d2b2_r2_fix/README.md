# D1G-D2B2-R2-FIX

PASS_NONSPAWNING. Production terminal remains OFF.

Only terminal_runtime.py's full_process_image_path and liveness ownership
validator changed; new tests are test_terminal_process_evidence_validators.py.
Concrete signaled/not-signaled state requires an acquired process handle, even
with a root label. UNKNOWN and failed/partial observations remain representable.
UNC structure requires nonempty server, share and executable components, including
extended UNC paths. Drive-absolute, extended drive and volume-GUID file paths
remain representable. Paths are preserved; no image authorizes continuation.

Before fixes: both liveness cases and the malformed UNC reproduction failed
the new expected-rejection tests (3 expected failures). After fixes: 3 passed.
Focused final: 95 passed (41 prior attestation cases + 54 new validator cases).
Ordinary full with launch guards: 1740 passed. Guarded full: 1740 passed.
All runs recorded zero launch/audit/native-launch-symbol events and retained
the _winapi guard. Full guarded mode also prohibits live binding loading.
The direct official Python hosts were started without a shell.

validation.json contains exact executable/argv, run timestamps, counts, guard
results, source hashes and report/log hashes. Existing runner phase labels are
retained; the aggregate identifies this FIX phase. Initial failure evidence is
preserved in reproduction_before.json/.log. No original report was overwritten.

All 25 prior preservation artifacts and all 12 original R2 diagnostic files
remain byte-identical. Ledger, native backend/P1, containment, target/v2,
approval/protected authority and production registration remain unchanged.
Git index unchanged; no staging, commit or push.

Pure/fake validation only: no live terminal child or native process control,
no recovered PID 5888 identity, no proven snapshot-to-open continuity. Parent
PID is still deferred. This result provides no permission for live execution.
