# Terminal runtime boundary (D1G-A / D1G-C)

D1G-A implements preparation, approval identity and continuation to injected
recording handlers only. D1G-C registers terminal local approval presentation,
but no terminal capability or runtime is registered in production. Production
terminal pending state remains unreachable. The following policy is REQUIRED
for D1G-D; none
of the launch/capture/containment mechanisms below is implemented in D1G-A.

## Prepared authority

One Windows terminal step has exactly three literal arguments: executable,
ordered argv and absolute cwd. Executor owns when preparation happens; terminal
capability code owns how. A planner AUTOMATIC proposal still requires human
confirmation. FORBIDDEN and critical risk block before preparation. A supplied
confirmed step without the matching stored pending target fails closed.

The single canonical ApprovalSubject projection includes every declared
TerminalExecutionTarget field. argv and environment pairs become explicit JSON
lists. All containers are detached. Environment source insertion order is
canonicalized to the fixed allowlist order; changing values changes identity.
Reordering an already frozen environment violates v1. The generic pending
envelope retains the same immutable target object; resume never prepares again.
Browser dispatch uses the prepared URL; terminal dispatch uses {"target": target}.
Orchestrator does not parse either capability's target.

## Required future Windows launch policy

- Use the absolute resolved executable; no launch-time PATH search or fallback.
- Revalidate executable_identity immediately before launch. Digest mismatch,
  disappearance or unreadability must fail closed. Never replace the approved
  digest with a newly observed one or select another executable.
- Deterministic argv serialization must follow the Windows CRT quoting rules:
  preserve order and empty arguments, quote whitespace, and escape backslashes
  before quotes and at the end of quoted arguments. Test quotes, trailing
  backslashes, Unicode and empty strings against the later supported runtime.
  The exact executable is separate from serialized argv. This does not promise
  compatibility with an arbitrary executable's custom command-line parser.
- shell=False. Deny known shell entrypoints and resolved batch targets. Inspect
  requested AND final resolved filenames, including aliases/links. No command
  string, shell wrapping, interactive stdin or detached execution is supported.
- Use the exact prepared cwd and exact frozen environment without merging fresh
  ambient variables. Bind stdin=DEVNULL and the fixed 30-second target timeout.
- Bound stdout/stderr independently to 1 MiB retained bytes each. Drain without
  unbounded accumulation and expose stdout_truncated/stderr_truncated flags.
- Decode deterministically as UTF-8 with replacement for invalid sequences;
  record encoding and decoding policy in execution metadata. No locale guessing.
- Preserve exit_code, stdout, stderr, timed_out, truncation flags and execution
  metadata on FAILED as well as COMPLETED outcomes.
- Bound cleanup to 5 seconds after timeout/failure. Terminate and reap the
  contained process tree; if cleanup cannot be proven, fail closed and report
  the uncertainty. No unbounded wait may replace this deadline.
- Require a Windows Job Object with kill-on-close and no breakaway. Establish
  containment before child code can run (suspended launch, assignment, then
  resume); assignment failure must terminate the child and fail closed.
- Restrict inherited handles to an explicit allowlist of required standard I/O
  handles. Ambient inheritable handles must not escape to the child.

These launch policies require a tested Windows implementation before activation.
D1G-A documentation and inert tests do not establish them as runtime guarantees.

## Executable identity / TOCTOU

The approved target contains executable_identity. Rehash-before-launch alone
does not prove approved binary bytes equal executed binary bytes: a file can
change between revalidation and image loading. Binary-byte continuity remains
unproven until a later tested Windows mechanism establishes it. No process code
is added to address TOCTOU in this phase.

## Shell and argument security boundary

The denylist is not a semantic sandbox for arbitrary executables. The guarantee
is exact process-creation inputs, not all resources later consumed by the
executable. python.exe script.py binds the interpreter executable, argv text,
cwd and environment; script.py contents remain mutable and are not hashed.
git.exe status depends on mutable repository/config state. Do not hash arbitrary
argv-referenced files in this contract. Shell-looking argv strings remain literal
text, without expansion by Jarvis.

## Structured handler results

An ordinary mapping continues to mean COMPLETED. A handler may return the
existing StepExecutionResult with COMPLETED or FAILED and structured data.
Executor validates its step number, capability, status, error and mapping keys;
FAILED stops the plan while retaining data in ExecutionReport and transport.
Generic exceptions still provide an error string; runtimes that need structured
failure data must return the explicit FAILED result rather than throw it away.
No stdout/stderr capture code is implemented here.
