# Terminal runtime contract (D1G-D1a, non-executing)

D1G-A established preparation/approval/recording continuation. D1G-C registered
terminal local approval presentation only. D1G-D1a hardens pure contracts.
No launch/capture/containment implementation exists here. No terminal process
handler, default runtime, planner activation or bootstrap execution wiring.
D1G-D1b implements an unregistered backend; D1G-D2 is the first real process gate,
D3 adversarial tests, D4 lifecycle/readiness and D1G-E explicit activation.

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

One fresh Job Object per invocation. Containment AT creation via an appropriate
creation-time Job-list mechanism; kill-on-last-handle-close, no breakaway,
noninheritable Job handle. Ordinary descendants remain contained. Root exit is
not cleanup completion: remaining descendants require termination AND FAILED.
Unsupported/nested-Job failures never fall back to uncontained launch. Brokered
service/task/WMI work and malicious same-user interference are outside the
ordinary-descendant ownership guarantee, not a sandbox.

Restrict inherited handles with an explicit allowlist: stdin NUL, stdout writer,
stderr writer ONLY. Never Job/process/thread/executable-verification/cwd-
verification/parent-read-pipe/synchronization/event handles. A BLOCKER before
D1G-D2: real sentinel-handle and concurrent-creation inheritance tests.

Symbolic flags: CREATE_SUSPENDED (setup before resume),
EXTENDED_STARTUPINFO_PRESENT (explicit handle/Job lists),
CREATE_UNICODE_ENVIRONMENT (frozen Unicode block), CREATE_NO_WINDOW (console
detachment). No DETACHED_PROCESS/CREATE_NEW_CONSOLE/CREATE_BREAKAWAY_FROM_JOB,
debugger or process-group flags. No Win32 flags/APIs are executed here.

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
process_resumed, process_exited, failure_stage, reason_code, cleanup_completed,
termination_reason, descendants_terminated, verified_executable_identity (null
until verified), environment_identity, duration_seconds, serialization_policy,
decoding_policy, encoding/error policy, retained/observed counts, completeness.
Do not expose raw handles, full frozen environment or arbitrary local exceptions.

execution_started = successful creation. process_resumed = successful resume,
not proof main() ran/useful work occurred. process_exited = observed root exit;
cleanup_completed = root + Job cleanup confirmed.

| Condition | Status |
| --- | --- |
| Exit 0 + acceptable capture + complete cleanup | COMPLETED |
| Nonzero exit / timeout with partial data | FAILED |
| Integrity mismatch/missing/unreadable executable, zero creation calls | FAILED |
| Invalid handler target / creation API failure | FAILED |
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
