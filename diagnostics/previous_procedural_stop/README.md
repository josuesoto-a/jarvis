# D1G-D2A LIVE WIN32 NON-SPAWNING DIAGNOSTICS RESULT

Overall result: **STOP — whole-turn non-spawning boundary violated.**

The isolated guarded diagnostic run passed every native check. However, an
earlier standalone official-interpreter verification command called
`platform.machine()` before installing a child-process audit guard. The locally
inspected official CPython 3.11 `platform.py` implementation calls
`subprocess.check_output(..., shell=True)` to run `ver` on this path. That
verification command returned successfully, so a preliminary shell helper launch
is inferred; its exact count was not instrumented. This is not Jarvis terminal
runtime execution, but it violates this phase's prohibition on helper children.
A whole-turn CreateProcessW zero-call proof therefore cannot be claimed.

The first guarded diagnostic attempt blocked the same fallback before native
dispatch and acquired no resources. Its evidence is preserved in
`d1g_d2a_host_guard_stop.json`. The corrected harness verifies architecture using
IsWow64Process2 and seeds only its interpreter-local platform cache from native
host facts. It keeps the child-process audit guard installed throughout.

Guarded run evidence in `d1g_d2a_result.json`:

| Check | Result |
| --- | --- |
| Host | Windows 10 build 19045 workstation; native AMD64, no WOW64; CPython 3.11.9 x64; explicit ABI layouts passed |
| Live backend and bindings | Constructed on one non-main owner; all backend symbols resolved; real Win32 APIs called |
| CreateProcessW | Symbol resolved; three tripwire attempts blocked; zero native invocations in the guarded run; receipt P0/call_count=0 |
| Child creation | No Jarvis terminal child; no helper child in the guarded run; preliminary unguarded probe used the shell fallback described above |
| Job | Created, kill-on-close configured and queried (0x2000), active count 0, released |
| Attributes | HANDLE_LIST exactly 3 distinct inheritable handles; JOB_LIST exactly 1; both updated; attribute list deleted and heap storage freed |
| NUL | Opened, duplicated for future stdin, both handles released |
| Pipes | Two local-only first-instance pipes; actual protected non-null DACLs each had one matching logon SID ACE with mask 0x0012019f; parent handles noninheritable; same-process clients verified; all resources released |
| Overlapped reads | Both pending reads completed with the exact in-process test bytes |
| CancelIoEx | Both new pending reads cancelled; final GetOverlappedResult observed ERROR_OPERATION_ABORTED (995) before dependency release |
| Executable | C:\Users\josue\AppData\Local\Programs\Python\Python311\python.exe; held GENERIC_READ/FILE_SHARE_READ; competing write/delete opens denied with sharing violation; native final path matched; supported local disk |
| Size / PE | 103192 bytes; machine 0x8664 AMD64; subsystem 3 console; bounded PE contract accepted |
| SHA-256 | 5f7b89a612c9b8af1d6456cdfcd1dbe5ca630849e79aebced9bee9a6694952ec |
| Cwd | C:\Users\josue\jarvis; native final-path local disk/directory verification passed |
| Cleanup | 14 handles closed, 4 LocalFree allocations freed, 3 heap allocations freed, 1 attribute list deleted; native outstanding counts 0; 25 published owners released, 2 unacquired creation slots reserved; no pending operations or borrows |
| Domain | OPEN; no quarantine or poison; no native cleanup uncertainty |
| Ordinary tests | Separate existing .venv suite: 1510 passed in 18.34s; focused fake D1b-related tests: 572 passed in 5.07s |
| Protected core | All previously tracked files and index unchanged; no registration or AUTORIZAR runtime wiring; existing .venv not rebuilt or modified by the harness |
| Target/v2 | Prepared identity matched held native SHA-256; terminal-execution-target/v2 validation passed; production job policy remains unresolved-no-activation/v1 |
| D2b | Not performed; candidate data preserved as an unapproved, withheld proposal; readiness withheld because overall phase boundary failed |

Repository inspection confirmed a clean `main` at
`8fafec002bff0f465e36e79213a7f42563330e6a`, equal to `origin/main`.
Only this diagnostic script and evidence directory were added as untracked files.
Nothing was staged, committed, or pushed.

The script's PASS/STOP result is scoped to its guarded interpreter. The saved
phase result additionally records the preliminary violation and remains STOP.
Passing diagnostics do not establish process creation, resume, containment,
production activation, or D2b approval.
