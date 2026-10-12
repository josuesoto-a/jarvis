# D1G-D2B-R2A host validation evidence

HEAD and origin/main remain 722e4d0513d56f40853187e576c682ac9ca33691.
The pre-existing D2b/R2 working tree, STOP evidence and failed R2 guarded_full.json
are byte-preserved. See validation.json for initial/final Git status and SHA-256
comparisons. No staging, commit, push, D2b2 or production activation occurred.

The previous authoritative architecture path used platform.machine(). On the
installed Python 3.11 implementation, machine() calls uname(), which calls
win32_ver(), _syscmd_ver(), subprocess.check_output(), subprocess.Popen(), and
cmd.exe /c ver. Architecture text comes from PROCESSOR_ARCHITEW6432, falling back
to PROCESSOR_ARCHITECTURE. The existing policy accepts only os.name == nt, Windows
major 10, build >=10240, workstation product type 1, case-insensitive AMD64 or
X86_64 machine text, and sys.maxsize > 2**32.

The contract concerns BOTH native AMD64 OS architecture and 64-bit Python.
The implementation retains the OS/version/product/interpreter checks and replaces
architecture text with direct in-process kernel32 evidence. IsWow64Process2 returns
the native IMAGE_FILE_MACHINE code; only AMD64 (0x8664) passes. GetNativeSystemInfo
can report emulated architecture on ARM64, so it is used only when the newer
symbol is unavailable on a pre-1709 build (<16299). This preserves the original
10240 floor. Its SYSTEM_INFO layout uses explicit scalar and pointer widths, and
only PROCESSOR_ARCHITECTURE_AMD64 (9) passes. API load/query failures, missing
modern APIs, and unsupported/unknown/unwritten output reject preparation. A call
failure never enables fallback. Environment text cannot override native evidence.

ctypes is imported lazily inside the host query. Importing capabilities.terminal
loads no native library, terminal backend or runtime, and registers no capability.
The query resolves only IsWow64Process2 or GetNativeSystemInfo; it does not resolve
any process-launch symbol. Production registration, target/v2, approval projection,
ApprovalSubject, PermissionEngine, AUTORIZAR, atomic claim, Planner and Executor
authority remain unchanged.

Validation uses installed CPython 3.11 directly with -I -S -B and the existing
.venv site-packages. The venv launcher/Store alias could not start. No shell was
used for test-host startup. The existing R2 guarded runner is byte-unchanged.

| Check | Final result |
| --- | --- |
| Compile changed Python files | PASS (3 files; no bytecode writes) |
| Host focused | 46 passed |
| Guarded required focused regressions | 898 passed |
| Terminal contract / runtime contract | 18 / 147 passed |
| Terminal approval / pending identity / local approval | 90 / 8 / 63 passed |
| R2 PID evidence | 53 passed |
| Runtime lifecycle / runtime Win32 / native contract | 172 / 72 / 181 passed |
| Action worker | 48 passed |
| Ordinary full without XML | 1633 passed, exit 0 |
| Guarded full without XML | 1633 passed, pytest_exit_code 0 |
| Clean guarded blocked process/helper events | 0 |
| Clean guarded launch API attempts | 0 |
| Clean guarded native launch-symbol attempts | 0 |
| no_live_launch_evidence | true |
| Real terminal backend children | 0 |
| Production terminal | OFF |

The initial guarded focused attempt failed because a newly tightened static test
mistook an existing local PE variable named machine for a prohibited API. Its
report/log are preserved as guarded_focused_initial.*. The test was corrected;
process guards were never weakened.

A reporting deviation is preserved in xml_reporting_attempt.json and associated
logs/XML files. Optional pytest JUnit XML finalization calls platform.node(), which
reaches the helper-capable platform path. The guarded attempt blocked
subprocess.Popen after test execution. The earlier ordinary XML attempt was
unguarded, so zero helper-launch claims cannot be made for that attempt. Both full
suites were rerun without XML, with no report metadata workaround or guard change.
The clean guarded report proves zero events inside the guarded pytest host;
command-runner test-host startup is excluded, as stated by the existing runner.

The clean evidence supersedes prior failed validation for current code; it does
not erase prior evidence. R2A and R2 fake regression gates pass. This grants no
D2b2 or live production activation authorization.

API references:
- https://learn.microsoft.com/en-us/windows/win32/api/wow64apiset/nf-wow64apiset-iswow64process2
- https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getnativesysteminfo
- https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getcurrentprocess
