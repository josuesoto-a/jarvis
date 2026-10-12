# D1G-D2B first real Win32 child: STOP

The sole authorized live invocation created exactly one native child. No retry
was performed. The persistent single_attempt_reserved.json marker is retained.
result.json is the original diagnostic result; validation.json records independent
read-only checks after the run. Production terminal remains OFF.

Base: main, HEAD and origin/main 722e4d0513d56f40853187e576c682ac9ca33691;
worktree was clean before implementation. All existing tracked files remain
unchanged, including the backend, runtime, protected core and target/v2.

The exact frozen D2a proposal was preferred as instructed. It uses the verified
Python311 executable, repository cwd and frozen environment, with -I -S -c and
the script:
    import sys; sys.stdout.write("D2b stdout\\n"); sys.stderr.write("D2b stderr\\n")

Frozen expectations were LF-only. Actual capture was D2b stdout followed by CRLF
and D2b stderr followed by CRLF. utf8-replace/v1 does not normalize newlines.
Consequently exact capture validation fails, despite matching visible line text.
The harness has not been changed to accept those bytes.

The root exited with code 0. After root exit, Job accounting reported one active
process. The existing runtime classified this as descendants_outlived_root,
recorded a cleanup failure and called TerminateJobObject once. The next Job
observation was zero. The evidence does not identify which process was still
counted and cannot prove that the count represented an actual descendant rather
than an exit/accounting transition. No policy or runtime workaround was made.

PROVEN LIVE for this single attempt:
- Explicit CreateProcessW dispatch once; known success once; no failure or CU.
- Existing suspended flags and JOB_LIST supplied at creation; IsProcessInJob
  confirmed membership before the single ResumeThread result of 1.
- HANDLE_LIST exactly three inheritable child ends; all other owned handles
  checked noninheritable. STARTUPINFOEX, application, mutable command, complete
  frozen Unicode environment block and cwd checked before dispatch.
- P1 committed before output inspection and ownership adoption, using the
  unchanged backend commit sequence and an instrumentation callback whose first
  statement invokes the original payload-free record_success.
- Independent stdout/stderr capture and EOF, root exit, eventual Job zero.
- All 16 acquired handles, three heap allocations, two local allocations and
  one initialized attribute list released; pending I/O and borrows zero.
- Verification handles released at their existing lifecycle point; Job LAST.
  Domain OPEN, no quarantine or poison. Owner non-main/non-daemon and joined.
- No helper launches within the guarded diagnostic interpreter.

NOT PASSED:
- Exact frozen output bytes (CRLF was observed, LF expected).
- Normal completion without Job termination/cleanup failure.

STILL UNPROVEN / DEFERRED:
- Identifying the post-exit active Job process/accounting transition.
- Child-side sentinel and concurrent inheritance/adversarial runtime cases.
- Forced timeout, forced cancellation, crashes, live CU fault injection.
- Shutdown with an active process, numerical Job resource ceilings.
- Production AUTORIZAR path, D3, D4 and production activation.

Ordinary regressions were separate: 1534 passed in 11.52 seconds, comprising
the 1510 baseline and 24 new non-spawning diagnostic tests. Three new tests
exercise the actual diagnostic backend definitions against in-memory Win32
P1/C1/CU doubles; they never import main or load native bindings.
The ordinary suite's pre-existing unrelated isolated-import interpreter helpers
are outside the live diagnostic's process-count scope, as in D2a.

The live command already executed once was:
    Python311/python.exe -I -S -B scripts/live_d1g_d2b_first_child.py --acknowledge D1G-D2B-ONE-CHILD

This is historical execution evidence, not authorization to rerun. Do not remove
or bypass the persistent marker. Further live execution requires a separately
reviewed and authorized phase. D1G-D3 readiness is BLOCKED by this D2b STOP.
No staging, commit or push occurred.

