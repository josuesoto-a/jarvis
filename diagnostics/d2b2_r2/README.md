# D1G-D2B2-R2: non-root process attestation implementation

Status: IMPLEMENTED_FAKE_VALIDATED_NONSPAWNING. Production remains OFF.
No real process-control or process-attestation API was invoked. No terminal
child, retry, D2B3, D3 or D4. Direct tool-side official Python test hosts and
read-only Git inspection are excluded from guarded-host child/helper counts.
No shell was used.

Immutable NonRootProcessEvidence records requested/confirmed PID, full image
path, exact integer creation FILETIME, zero-time signaled/not-signaled/unknown
state, membership of the opened object in the exact known Job, per-stage
issues, sequence/interval and confirmed owner release. No raw handle escapes.
Parent PID is deferred with None/source/issue metadata, without a fallback.

32 single-use observation owner wrappers are reserved/adopted during preflight.
Their later OpenProcess acquisition belongs to the existing ledger owner;
reserve/adopt restrictions remain byte-identical. Unused wrappers release
without native close. Capacity exhaustion is explicit observation uncertainty,
not a Job process-count limit. OpenProcess requests only 0x00101000 with FALSE
inheritance. No escalation, shell, Toolhelp or NtQueryInformationProcess fallback.

The same acquired handle supplies GetProcessId, QueryFullProcessImageNameW,
GetProcessTimes, WaitForSingleObject(handle,0) and IsProcessInJob. PID mismatch
stops normal queries; a changed observed generation is an explicit identity
race. First-snapshot continuity remains SNAPSHOT_LINK_UNPROVEN even when the
current opened process object is attested. Failed queries preserve earlier
valid facts. A conhost/OpenConsole name is a possible-role hypothesis only;
no classification permits continuation. Containment/failure policy is unchanged.

Runtime and native entry reject CU attestation. Release uses the existing
single-attempt ledger path; unconfirmed acquisition/release retains ownership
and invokes existing quarantine/poison. Existing deadlines/cancellation are
checked without reset or grace. Job remains last. Root exit, accounting zero,
and each stream EOF remain independent. The original P1 method is byte-identical.

Validation: syntax 7 Python files; ordinary with launch guards 1686 passed;
guarded full 1686 passed. This includes 41 attestation tests plus baseline 1645.
All final reports have zero audit/launcher/native-launch-symbol events and
retained launch guards. The initial focused failure was an outdated assertion
excluding GetProcessId. That evidence is preserved; its replacement checks
explicit ABI, while root identity tests prove no GetProcessId root query occurs.

All 25 preserved D2b/D2B2 evidence and D2B2 implementation/test files remain
byte-identical. Both STOP results and consumed markers remain untouched.
This phase cannot recover historical PID 5888 provenance and does not claim
live native attestation compatibility.

Base main HEAD/origin/main 18076ec1d02ad5d467129401c38763bf224750b3.
Target/v2, creation flags and protected authority remain unchanged.
No stage, commit or push; Git index byte-identical. See validation.json for
source/evidence hashes and final guarded reports.
