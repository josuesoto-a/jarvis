# D1G-D2A CLEAN LIVE WIN32 NON-SPAWNING REVALIDATION RESULT

Clean revalidation result: **PASS.** One dedicated guarded D2a interpreter
completed with zero helper children and zero native CreateProcessW dispatches.
All required native diagnostics and cleanup checks passed. D2b is eligible for
separate authorization; it was not performed or authorized in this task.

This README and `d1g_d2a_result.json` represent the new clean revalidation.
The previous procedural STOP remains STOP: byte-identical copies of its report,
JSON, harness, and host-guard STOP evidence are preserved in
`previous_procedural_stop/`. The earlier unguarded host probe's shell/helper
fallback is not reclassified as passing. The top-level
`d1g_d2a_host_guard_stop.json` also remains unchanged historical evidence.

The clean harness starts with a child-process audit hook, then non-forwarding
guards for Python launch APIs, including _winapi.CreateProcess, os.system and
os.popen. Real subprocess and platform modules are not loaded or invoked.
Fail-fast sentinel modules satisfy inert dependency imports; all platform APIs
remain forbidden. Guarded DLL loaders resolve CreateProcessW but expose only
the tripwire. The guard checks remained installed through diagnostic completion.

Host validation uses sys facts and IsWow64Process2. The two isolated diagnostic
module aliases for host validation use a direct-native adapter preserving the
production host policy. No committed production source or target policy changes.

Clean-run evidence in `d1g_d2a_result.json`:

| Check | Result |
| --- | --- |
| Host | Windows 10 build 19045 workstation; native AMD64, no WOW64; CPython 3.11.9 x64; explicit ABI layouts passed |
| Live backend and bindings | Constructed on one non-main owner; all backend symbols resolved; real Win32 APIs called |
| CreateProcessW | Symbol resolved; three tripwire attempts blocked; zero native invocations in the guarded run; receipt P0/call_count=0 |
| Platform / subprocess | No real module use; platform API calls 0; Python launcher attempts 0; child-creation audit events 0 |
| Child creation | Helper children 0; subprocess/shell children 0; terminal children 0; external executable calls by the diagnostic 0 |
| Owner thread | Explicit non-main, non-daemon thread; deterministic successful join; owner failures propagate to main after saved STOP evidence; no sleeps used for correctness |
| Job | Created, kill-on-close configured and queried (0x2000), active count 0, released |
| Attributes | HANDLE_LIST exactly 3 distinct inheritable handles; JOB_LIST exactly 1; both updated; attribute list deleted and heap storage freed |
| NUL | Opened, duplicated for future stdin, both handles released |
| Pipes | Two local-only first-instance pipes; actual protected non-null DACLs each had one matching logon SID ACE with mask 0x0012019f; parent handles noninheritable; same-process clients verified; all resources released |
| Overlapped reads | Both pending reads completed with the exact in-process test bytes |
| CancelIoEx | Both actual pending reads cancelled; request remained distinct from completion; final native GetOverlappedResult returned FALSE/error 995 and completed the ledger operations before dependency release |
| Executable | C:\Users\josue\AppData\Local\Programs\Python\Python311\python.exe; held GENERIC_READ/FILE_SHARE_READ; competing write/delete opens denied with sharing violation; native final path matched; supported local disk |
| Size / PE | 103192 bytes; machine 0x8664 AMD64; subsystem 3 console; bounded PE contract accepted |
| SHA-256 | 5f7b89a612c9b8af1d6456cdfcd1dbe5ca630849e79aebced9bee9a6694952ec |
| Cwd / locality | C:\Users\josue\jarvis; native final-path disk/directory verification passed; executable and cwd roots observed as DRIVE_FIXED (3) |
| Cleanup | 14 handles closed, 4 LocalFree allocations freed, 3 heap allocations freed, 1 attribute list deleted; native outstanding counts 0; 25 published owners released, 2 unacquired creation slots reserved; no pending operations or borrows |
| Domain | OPEN; no quarantine or poison; no native cleanup uncertainty |
| Ordinary tests | Separate existing .venv suite: 1510 passed in 8.84s; its pre-existing verification-interpreter behavior is outside the dedicated live diagnostic proof |
| Protected core | All previously tracked files and index unchanged; no registration or AUTORIZAR runtime wiring; existing .venv not rebuilt or modified by the harness |
| Target/v2 | Prepared identity matched held native SHA-256; terminal-execution-target/v2 validation passed; production job policy remains unresolved-no-activation/v1 |
| D2b | Not performed; proposed target data prepared only; eligible for separate authorization; production activation remains off |

Repository inspection confirmed `main` at
`8fafec002bff0f465e36e79213a7f42563330e6a`, equal to `origin/main`.
Tracked files and index were unchanged at start; prior D2a artifacts were untracked.
Only those diagnostic artifacts were revised or copied into the historical archive.
Final Git status is `?? diagnostics/` and `?? scripts/live_d1g_d2a_diagnostics.py`.
Nothing was staged, committed, or pushed.

Proposed D2b target only:

- Executable: the exact official Python path and identity verified above.
- Cwd: C:\Users\josue\jarvis.
- Argv: `-I`, `-S`, `-c`, followed by
  `import sys; sys.stdout.write("D2b stdout\n"); sys.stderr.write("D2b stderr\n")`.
- Expected future observations: exit 0; stdout `D2b stdout\n`; stderr `D2b stderr\n`.
- Full frozen target/v2 fields and environment projection are in the result JSON.

The official interpreter hosted diagnostics only. No candidate command was run
through the terminal backend. Passing diagnostics do not validate process
creation, resume, child containment, child output capture, or production activation.
The clean-run proof excludes the external command runner and the separate ordinary
test process. It does not claim the entire session's tools/tests created no processes.
