# Terminal runtime contract (D1G-D1a / D1G-D1b0 / D1G-D1b1a, non-executing)

D1G-A established preparation/approval/recording continuation. D1G-C registered
terminal local approval presentation only. D1G-D1a hardens pure contracts.
D1G-D1b0 adds pure lifecycle/ownership accounting in terminal_runtime.py.
D1G-D1b1a formalizes creation uncertainty and independent Job-empty evidence.
No launch/capture/containment implementation exists here. No terminal process
handler, default runtime, planner activation or bootstrap execution wiring.
D1G-D1b implements an unregistered backend; D1G-D2 is the first real process gate,
D3 adversarial tests, D4 lifecycle/readiness and D1G-E explicit activation.

## D1G-D1b0 lifecycle and ownership source of truth

This foundation records supplied evidence using opaque Python tokens. It does
not acquire, release, launch, resume, wait for, or terminate OS resources. There
is no backend, executing runtime, handler adapter, registration, or activation.
Target/preparation/approval versions and identity-bound policies are unchanged.

LaunchAdmissionDomain is explicit and injectable. Every future production
runtime must share one durable process-local domain supplied by its factory;
constructing a new runtime with that domain cannot bypass poisoning. D1b0 does
not create a production singleton or claim to prevent a caller from deliberately
constructing a separate isolated domain. Tests use fresh domains, never shared
global state. Admission is not authorization; atomic approval claim stays with
the Orchestrator.

| Domain state | Admission / cancellation / shutdown |
| --- | --- |
| OPEN | One invocation may be admitted |
| RUNNING | Another admission fails; cancellation may be requested |
| POISONED | No admission; retained quarantine remains available for remediation |
| CLOSED | Admission permanently closed; cancellation/shutdown idempotent |

Shutdown during RUNNING closes admission and requests cancellation but does not
claim CLOSED while ownership remains active. POISONED has precedence over
shutdown and never changes to CLOSED, even after successful remediation.
cancel_active() only requests cancellation of the active invocation; shutdown()
also permanently closes admission. Both are nonblocking requests. Neither runs
cleanup, joins a worker, invokes a backend, or claims a five-second completion.
Snapshots report incomplete closure until a nonpoisoned domain is actually idle
after shutdown. Application shutdown wiring remains D4.

The domain strongly retains its active invocation. Quarantine preallocates a
separate QuarantineOwner, latches poison, then transfers responsibility by one
domain quarantine-reference assignment before clearing the active lease. That
single assignment both retains the successor and defines ledger authority and
QUARANTINED phase, including after an interruption in subsequent bookkeeping.
The old ledger remains inspectable but cannot acquire, borrow, release, confirm
release or mark uncertainty after transfer. Only the quarantine owner may record
release/remediation. Duplicate transfer is rejected. Existing borrow leases and
pending-operation completion records may discharge their retained obligations;
they do not release ownership. Receipts remain late-fact channels for the new
owner. Caller deletion cannot drop these resources while the shared domain lives.
This is in-process durability, not restart recovery or an interpreter-teardown/
native-memory guarantee. Poison has no reopen/reset operation in v1: restart
is the documented recovery boundary, never implicit construction of a runtime.

| Invocation phase | Next ordinary phases |
| --- | --- |
| PREFLIGHT | CREATE_COMMIT, CLEANING |
| CREATE_COMMIT | CREATED_SUSPENDED, CLEANING |
| CREATED_SUSPENDED | RUNNING, CLEANING |
| RUNNING | CLEANING |
| CLEANING | FINISHED, QUARANTINED |
| FINISHED | None |
| QUARANTINED | None; late evidence and remediation may be recorded |

There is no general phase setter. Named operations enforce evidence gates. The
budget starts at admission before preflight. Creation commit and begin_call()
both check cancellation/deadline; commit_resume() provides the analogous final
model gate before one future resume attempt. Cancellation after a gate can race
an already-committed native call: successful evidence must still be recorded.
One cleanup deadline is established on entering CLEANING and never reset.
First cleanup reason is retained; later timeout does not overwrite a timely
root-exit decision. A root exit first observed at/after the execution deadline
is conservatively classified as timeout; exact OS exit chronology is not known.

| Creation classification | Exact recorded evidence |
| --- | --- |
| P0 / PRE_CREATE | Zero creation-call attempts |
| C1 / CREATE_CALL_FAILED | One attempt, explicitly reported failure; result no longer pending |
| P1 / PROCESS_CREATED | One attempt, explicitly reported success; ownership mandatory |
| CU / CREATE_OUTCOME_UNCERTAIN | One attempt with unavailable/uncommitted result; success cannot be excluded |

The preallocated CreationReceipt has reserved root-process and primary-thread
owners. record_success() permanently records native success BEFORE resource
wrapper validation/attachment. attach_process()/attach_thread() attach each
distinct opaque payload to its already-registered owner exactly once. The
optional pair convenience form also records success before pair validation;
failure there stays P1 with incomplete adoption. The future native bridge must
use immediate success recording before any fallible marshaling. Interruptions
after success, either attachment or phase adoption preserve P1 and every known
resource. Missing attachment is explicit and blocks FINISHED, including when
root exit and empty containment have been reported. Success cannot be overwritten,
failed, retried or reset. An unresolved call is neither C1 nor safe completion;
late evidence in quarantine does not reopen admission or change terminal phase.
Creation classification is derived from guarded receipt evidence and is exposed
by TerminalRuntimeOutcome.creation_classification. process_created means
successful creation has been confirmed. False in CU is NOT definite absence of
a process; C1 and CU are distinguished by classification and result_pending.
The scalar pending guard constructs no snapshot before the success latch.
Prebound process/thread identities exist before the attempt; adoption uses those
slots without requiring new logical identities after success. This narrows the
Python commit path but does not prove allocation-free execution or atomicity
with a future native call. Native storage integration remains D1b work.

Recording the first attempt exposes CU until an authoritative result is recorded.
Known failure resolves that pending evidence to C1; known success permanently
latches P1 before optional payload validation/attachment. CU itself never permits
resume, retry, successful completion or admission restoration. Reporting Job
emptiness, releasing known resources or reaching a deadline cannot resolve CU.
Receipt late-fact channels remain available after quarantine: actual late return
evidence may resolve the receipt, but never changes the terminal phase, permits
resume, makes cleanup_completed true or reopens the poisoned domain.

ResourceLedger reserves ownership slots before acquisition. adopt() attaches
one opaque payload immediately; raw scalar handle values are not the ownership
representation. Domain-scoped ResourceIdentity includes a monotonically issued
generation and resource kind/role. Backend value reuse with fresh opaque tokens
does not reuse resource identity. Slots/payloads cannot be replaced or readopted
within the ledger after release. Foreign ledger owners are rejected. A future
backend must issue fresh opaque tokens per acquisition, never alias live native
ownership across domains. Snapshots expose no payloads.
Acquisitions are limited to preflight; creation uses its already-reserved slots.

Ownership states are RESERVED, OWNED, RELEASING, RELEASE_UNCERTAIN, RELEASED and
QUARANTINED. BorrowedResource is a lease rather than another ownership state.
Release is prohibited while borrowed or referenced by a pending operation.
begin_release() records the one attempt and returns its opaque payload for an
external backend release outside the lock; confirm_released() records supplied
success. mark_release_uncertain() retains identity and prevents blind retry.
Independent later proof may confirm release without performing a second call.
mark_still_owned() records definite release failure as OWNED, rather than
RELEASE_UNCERTAIN. The original one-attempt receipt remains latched; there is
no automatic or blind retry, and unresolved ownership requires quarantine.
No destructor or release callback runs automatically.

PendingOperation retains every dependency until confirm_completed(). A
cancellation request is not completion and does not permit release. Quarantine
preserves pending operations, in-progress releases, uncertainty and creation
receipts. Remediation can confirm completion/release, but cannot create new
operations, acquire resources or turn QUARANTINED into FINISHED.

FINISHED requires a resolved, fully adopted creation receipt, all acquired resources released,
no borrows/pending operations, and (for P1) observed root exit plus confirmed
empty containment. Root exit alone is insufficient. job_empty is a separate supplied fact from
process_exited and containment_empty. confirm_job_empty() accepts P1 or CU in
CLEANING/QUARANTINED without requiring root-exit evidence. It does not imply P1,
C1, valid process/thread outputs or root exit. P1 still requires explicit root-exit
and containment confirmations; the separate Job fact cannot substitute for them.
CU with Job empty and every known resource released still has unresolved creation
obligations, cannot finish cleanup and must retain responsibility in quarantine.
The creation_uncertain first trigger cannot be rewritten by later Job evidence.
Unconfirmed cleanup
requires quarantine and poison; successful remediation reports resolution but
never clears poison. A backend must report accurate evidence: this pure model
does not independently verify supplied facts or perform containment.

CleanupEvidence derives cleanup_completed from terminal phase, receipt adoption,
resource states, borrow/I/O obligations and required lifecycle confirmations.
It cannot be supplied as an independent success Boolean. Any quarantine transfer
permanently keeps that invocation's cleanup_completed=False, even if explicit
remediation later resolves the quarantine. Release uncertainty is never successful
release; a second blind release attempt is prohibited. A definitely still-owned
resource stays OWNED until a release attempt has genuinely ambiguous evidence.

TerminalRuntimeOutcome is frozen and lifecycle-only: receipt, resume/exit facts,
derived cleanup evidence, first terminal trigger and bounded cleanup issues.
It has no Executor/ActionStatus dependency or fabricated output/exit code.
Its local COMPLETED disposition means a resumed root-exit lifecycle with complete
cleanup and no recorded failure, not proof of program exit zero or acceptable
capture. D1b must add actual execution/capture semantics before result projection.
Root exit may win the first-trigger latch, but a later cleanup failure records
separate issues and forces FAILED without rewriting that exit evidence.

All public evidence mutations share the domain condition lock. No backend
callback runs under it. Future external acquisition/release/wait operations
must run outside that lock and report their results through this contract.
The injected clock must be finite and monotonic. Frozen snapshots are detached
from later mutations and contain no request/step/approval authority.

D1b0/D1b1a terminal lifecycle/runtime code creates zero child processes, and no new
terminal-related test invokes a real backend or process. Existing unrelated
tests may launch their established isolated-import verification interpreter;
the complete repository is not claimed to create zero children.

## Reviewed v1 owner-thread and interruption guarantee

The future native terminal lifecycle must have one explicitly designated non-main
owner thread. The current ActionWorker thread is the natural intended owner;
D1b must enforce owner-thread affinity before resource acquisition. ActionWorker
is unchanged in D1b1a. Other threads may request cancellation/shutdown and signal
wake; they may not manipulate native resources, mutate capture, close handles,
terminate containment directly or inject exceptions into the owner thread.
Cancellation remains cooperative. Normal Python SIGINT handling runs on the
Python main thread; Ctrl+C is not asynchronous KeyboardInterrupt delivery to the
non-main lifecycle owner. Current worker shutdown does not inject exceptions.
Application cancellation/shutdown integration remains future work.

Under this supported model, creation storage and process/thread ownership slots
must be retained before exactly one native creation attempt. An observable known
successful return must become permanent P1 immediately before other fallible
lifecycle work. Cancellation and deadlines cannot erase P1. Python/native return
and receipt mutation are not claimed to be one atomic operation. If return
evidence is unavailable/uncommitted, CU is the honest classification: never
resume, retry, lose containment responsibility or reopen admission when successful
creation cannot be ruled out.

Future CU recovery must use the already-owned creation-time Job, rather than
trusting uncertain returned process/thread values. Confirming that Job empty
cannot fabricate root-exit or creation evidence and cannot heal unresolved CU
into healthy cleanup. Unresolved CU transfers to QuarantineOwner and poisons the
shared LaunchAdmissionDomain. No Job recovery API is implemented here.

Observed BaseException paths require cleanup/quarantine attempts where possible;
manual KeyboardInterrupt/SystemExit is distinct from ordinary main-thread signal
handling. Preallocation reduces avoidable allocation risk but does not guarantee
MemoryError recovery. Guaranteed recovery excludes hostile asynchronous exception
injection, catastrophic out-of-memory conditions preventing recovery code, abrupt
interpreter/process termination, native ABI corruption/access violation and
OS/kernel failure. No native shim is required for this supported v1 guarantee.

## Versions and prepared authority

capabilities/terminal.py owns the constants and immutable TerminalRuntimePolicy.
The ONE terminal_approval_arguments projection covers EVERY target field and
nested runtime-policy field. These declarations are not implemented controls.

| Contract | Version |
| --- | --- |
| Target | terminal-execution-target/v2 |
| Platform | windows10-11-x64-console/v1 |
| Environment | windows-minimal/v2 |
| Runtime | windows-bounded-runtime-contract/v1 |
| Serialization | windows-crt-argv/v1 |
| Output | windows-prefix-64k/v1 |
| Decoding | utf8-replace/v1 |

Former target/v1 and environment/v1 are not silently reinterpreted: old targets
fail validation and require new preparation/approval. The former documented
1 MiB policy is explicitly superseded by 64 KiB per stream under target/v2.
Material future policy changes require version changes; approvals must not gain
new semantics. All fixed policy fields are immutable and identity-bound.

One terminal step has exactly executable, ordered argv and explicit absolute
cwd, all literal-only. Executor owns when preparation happens; AUTOMATIC planner
proposals require confirmation, FORBIDDEN/critical block before preparation.
Fingerprint identifies data, not authority. Orchestrator owns atomic claim;
resume never prepares again. Terminal dispatch retains {"target": target}.
Browser preparation/dispatch and generic execution contracts remain unchanged.

## Supported host, paths and executable shapes

Initial host: Windows 10/11 x64 workstation, 64-bit Python; no Windows Server,
32-bit host, ARM/emulation or future major OS support. Version/product type and
reported architecture are checked without launching. Actual compatibility is a
D2/D3 gate. Paths are ordinary drive-absolute local Windows forms, max 259 UTF-16
units excluding NUL. Reject UNC/device/extended/NT namespaces, DOS reserved names,
alternate data streams, wildcards, controls and ambiguous trailing dots/spaces.
Strict resolution followed by final-path checks rejects mapped shares/reparse
targets resolving to UNC; supported local aliases may resolve to local paths.
This relies on Windows/Python final-path semantics, not hostile-driver protection.
Real mapped-drive/reparse behavior remains a D2/D3 requirement.

Bare names retain explicit preparation-time search of frozen PATH/PATHEXT;
relative PATH entries fail. No implicit cwd search. Use the absolute approved
path at runtime: no launch-time PATH search or fallback. The first existing
candidate fails on unsupported type/name/bytes rather than selecting another.

Validate requested AND final resolved filenames, including aliases/links.
Reject .bat/.cmd and known shell entrypoints; .exe/.com suffix alone is not proof
of native format. Read at most 64 KiB for structural PE inspection: x86 PE32 or
x64 PE32+, console subsystem, executable-not-DLL characteristics, standard
optional header, bounded section table/raw ranges and executable entrypoint
section. Reject malformed/truncated/legacy/GUI/managed/unsupported shapes.
This narrow check is not a complete loader, signature, dependency, malware,
safety or successful-execution verifier. It never executes code.

Regular-file metadata, inspected prefix and streaming digest use the SAME open
file during preparation. Executable size is bounded to 256 MiB; observed
size/mtime changes fail. Identity is exactly sha256: plus 64 lowercase hex
characters. Malformed casing/prefix/length/content fail, never normalize later.

## TOCTOU source of truth (future backend)

Revalidate executable_identity immediately before launch. Open the approved path
with restrictive read sharing where compatible, excluding write/delete sharing;
hash through the opened object, compare the approved digest, retain verification
handle through process creation. Mismatch/disappearance/unreadability/sharing
incompatibility/unsupported state must fail closed. Reconfirm supported object,
type and final path. Never replace the approved digest, re-prepare or substitute.

Binary-byte continuity remains unproven: this does NOT prove universal
byte-for-byte identity of what Windows ultimately loads. Ancestor redirection,
OS launch configuration, DLLs/plugins and launcher/interpreter dependencies
remain outside this contract. No restrictive-sharing/Win32 handle implementation
is added in D1G-D1a.

## Argv, cwd and exact environment

Deterministic argv serialization is windows-crt-argv/v1. Target argv contains
arguments AFTER the executable; derive from [executable_resolved, *argv].
Identity-bound executable supplies argv[0]. Preserve order/empty arguments/
whitespace with CRT space/tab quoting and backslash/quote escaping, pinned by
independent vectors. Serialized text is derived, not separate stored authority.
Maximum 1,024 arguments and 30,000 UTF-16 units INCLUDING final NUL, below the
Windows 32,767 ceiling. Reject NUL/unpaired surrogates. Arbitrary programs may
parse command lines differently.

shell=False; Jarvis introduces no shell mediation, command strings or wrappers.
The filename denylist is not a semantic sandbox and cannot detect every renamed
shell. Shell-looking argv stays literal. python.exe script.py binds input text;
script.py contents remain mutable. git.exe depends on mutable repository/config state.
Do not hash arbitrary argv-referenced files/scripts/repo state/configs/DLLs/
plugins/network responses in this authority contract.

Use the exact prepared cwd: canonical supported existing local directory, never
Jarvis-cwd fallback. Future prelaunch revalidation rejects missing/unreadable/
wrong-type or redirected unsupported cwd. Contents are not frozen; same-path
replacement and residual races are not universally detected. No directory ID
or filesystem snapshot is bound.

Allowlisted environment keys are selected case-insensitively: reject collisions
even for equal values. Freeze uppercase keys in fixed allowlist order.
SYSTEMROOT is mandatory and must identify a supported existing directory at
preparation. Do not add it after approval or expand secret-key allowlists.

Pass the exact frozen environment: no ambient merge, env=None inheritance,
PATH refresh or late additions. unicode-sorted-exact/v1 sorts canonical keys,
writes one KEY=VALUE\0 entry each plus final double NUL. Sorting is transport,
not identity ordering/value change. Limit: 32,768 UTF-16 units including
terminators. Pure target validation/builders perform no ambient/filesystem reads.
Allowlisted values are not certified secret-free.

## Future stdin, capture, decoding and deadlines

stdin=DEVNULL (Windows NUL), no PTY/inherited console input/password service.
Jarvis provides no interactive stdin; arbitrary programs may still show GUI
prompts, create consoles or communicate elsewhere.

Retain separate 64 KiB stdout/stderr byte prefixes, with fixed reads <=16 KiB.
No communicate()-style unbounded buffering; continue draining/discarding excess.
Report stdout_truncated/stderr_truncated, retained/observed counts and capture
completeness. Preserve order within each stream only, no cross-stream total order.
Pending reads require safe ownership/cancellation/completion; never abandon or
free their resources unsafely. No pipe code exists here.

Reviewed D1b v1 direction: local named byte pipes, overlapped parent reads,
synchronous child writers, zero reader threads, one pending read per stream.
Connection/security/ABI/cancellation safety must be proved in D1b/D2/D3; no
pipe or capture implementation is introduced by D1b0. These mechanics implement
the already-approved retention/decoding/containment policy; they do not add launch
authority or alter target/v2 identity. A material policy change still requires
explicit versioning, rather than silently reinterpreting an existing approval.

UTF-8 with replacement (errors="replace"), utf8-replace/v1: no locale/OEM
guessing, no child-environment mutation. Report encoding/error policy, byte
counts/truncation/completeness. OEM/ANSI/invalid output may lose meaning.
Display escaping remains presentation-owned.

30-second execution budget starts at runtime entry BEFORE preflight; verification
consumes it. Expiration before creation means no launch; afterward triggers
entire Job termination. One shared cleanup budget of 5 seconds covers
termination/waits/drains/verification on timeout, failure AND ordinary exit.
Windows/filesystem/kernel calls may stall: no strict universal wall-clock
completion guarantee. Unconfirmed cleanup fails, retains safe ownership and
disables further launches. No timers/processes implemented here.

## Future Job, handle, flags and resource controls

One fresh Job Object per invocation. Containment AT creation via
PROC_THREAD_ATTRIBUTE_JOB_LIST, CREATE_SUSPENDED and membership confirmation
before resume; no delayed-assignment fallback and no breakaway fallback.
Use kill-on-last-handle-close, no breakaway,
noninheritable Job handle. Ordinary descendants remain contained. Root exit is
not cleanup completion: remaining descendants require termination AND FAILED.
Unsupported/nested-Job failures never fall back to uncontained launch. Brokered
service/task/WMI work and malicious same-user interference are outside the
ordinary-descendant ownership guarantee, not a sandbox.

Restrict inherited handles with an explicit allowlist: stdin NUL, stdout writer,
stderr writer ONLY. Never Job/process/thread/executable-verification/cwd-
verification/parent-read-pipe/synchronization/event handles. Before the first
D1G-D2 child, require fake lifecycle/failure/ownership accounting, ABI review,
mocked native marshaling and handle-list/Job-list arguments, capture/deadline/
cancellation/poison tests and full regressions. D1b0/D1b terminal runtime tests
never invoke the live backend or launch terminal children. Existing unrelated
isolated-import interpreter tests are explicitly permitted and accounted for.
Only D2 may perform native preflight resource checks before its first controlled
child. Real sentinel-handle and concurrent-creation inheritance
tests require children and belong to D2/D3 before readiness/activation; they
cannot be prerequisites to the phase permitted to create the first child.

Symbolic flags: CREATE_SUSPENDED (setup before resume),
EXTENDED_STARTUPINFO_PRESENT (explicit handle/Job lists),
CREATE_UNICODE_ENVIRONMENT (frozen Unicode block), CREATE_NO_WINDOW (console
detachment). No DETACHED_PROCESS/CREATE_NEW_CONSOLE/CREATE_BREAKAWAY_FROM_JOB,
debugger or process-group flags. No Win32 flags/APIs are executed here.

Future ownership categories (abstract reservations today, not live handles):

| Resource category | Owner / future release boundary |
| --- | --- |
| Executable/cwd verification | Invocation ledger; noninherited, retained through creation |
| Job | Invocation ledger; noninherited, after containment confirmation |
| NUL/stdout/stderr child ends | Invocation ledger; exact three-entry inheritance list; parent copies released after creation |
| Parent pipe ends | Invocation ledger; noninherited, after read completion/cancellation confirmation |
| Process/primary thread | Preallocated creation receipt slots; process after exit, thread after confirmed resume handling |
| Read buffers/events/completion resources | Invocation ledger and explicit pending-operation dependencies; never freed while pending |
| Attribute-list backing resources | Invocation ledger; noninherited, retained through creation/attribute deletion obligations |

Backend primitives borrow resources and return supplied evidence; they do not
invisibly close runtime-owned resources. Compound partial construction must
register each acquired resource immediately. Backend-local temporary ownership
must also be explicit and transferred before a fallible return. There is no
garbage-collection, destructor, background reaper or automatic retry correctness
dependency. Uncertain release or unfinished native work transfers to quarantine
and poisons the shared domain, even for P0 before any process was created.

One active invocation per runtime instance; bounded inputs/retention and fixed
owned parent handles/two pending bounded reads. Job count/committed-memory
ceilings are UNRESOLVED, identity-bound as unresolved-no-activation/v1.
16 active processes / 512 MiB are candidates only: no real workload evidence
justifies adoption yet. D1G-D1b/D3 must select/validate actual ceilings and
version the policy/target before activation; old approvals cannot gain limits.
No CPU/disk/network/filesystem sandbox, and no bound on total request/result
retention elsewhere.

## Future results, status and failure atomicity

Keep D1G-A: ordinary mapping means COMPLETED; explicit StepExecutionResult
accepts COMPLETED/FAILED with structured data preserved through transport.
Do not broaden handlers to BLOCKED. No fake process results in D1G-D1a.

Public step data: exit_code (null before observed exit), stdout/stderr,
timed_out, stdout_truncated, stderr_truncated, execution_started, process_created,
process_resumed, process_exited, creation_classification, failure_stage, reason_code,
cleanup_completed,
termination_reason, descendants_terminated, verified_executable_identity (null
until verified), environment_identity, duration_seconds, serialization_policy,
decoding_policy, encoding/error policy, retained/observed counts, completeness.
Do not expose raw handles, full frozen environment or arbitrary local exceptions.

execution_started = confirmed successful creation; CU is explicitly uncertain.
process_resumed = successful resume,
not proof main() ran/useful work occurred. process_exited = observed root exit;
cleanup_completed = root + Job cleanup confirmed.

| Condition | Status |
| --- | --- |
| Exit 0 + acceptable capture + complete cleanup | COMPLETED |
| Nonzero exit / timeout with partial data | FAILED |
| Integrity mismatch/missing/unreadable executable, zero creation calls | FAILED |
| Invalid handler target / creation API failure / unresolved CU | FAILED |
| Resume/capture/cleanup failure / descendants outlive root | FAILED |
| Plan/policy/runtime availability blocked before handler | BLOCKED in Executor |
| Authorization absent | WAITING_FOR_PERMISSION outside runtime |

BEFORE creation any validation/policy/identity/cwd/environment/serialization/
resource-setup failure means ZERO process-creation calls. AFTER creation runtime
owns containment/cleanup across exceptions/interrupts, returns structured
failure, consumes authorization permanently and never retries automatically.
Retry requires a new prepared target and fresh approval.

## D4/E shutdown and activation

Future shutdown stops launch admission, terminates active Jobs, performs bounded
cleanup, returns structured worker outcomes, joins worker and reports incomplete
closure. Current production waits three seconds, suppresses errors and claims
closure: a known activation blocker, unchanged here.
Approval still truthfully says recording/tests only, no processes; preserve
wording until D1G-E. Real Windows tests, local result visibility, browser B1-B4/
approval regressions, manual keyboard AUTORIZAR smoke, explicit registration
and rollback are activation gates. Pure tests establish contracts only.

Sources: [PE format](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format),
[Windows creation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw),
[Job/handle attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute),
[Python final-path behavior](https://bugs.python.org/msg388754).
