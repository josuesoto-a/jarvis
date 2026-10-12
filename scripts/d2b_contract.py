"""Pure, non-spawning checks for the explicitly invoked D2b diagnostic."""
from capabilities import terminal as T

PYTHON = r"C:\Users\josue\AppData\Local\Programs\Python\Python311\python.exe"
DIGEST = "5f7b89a612c9b8af1d6456cdfcd1dbe5ca630849e79aebced9bee9a6694952ec"
CWD = r"C:\Users\josue\jarvis"
ARGV = ("-I", "-S", "-c", 'import sys; sys.stdout.write("D2b stdout\\n"); sys.stderr.write("D2b stderr\\n")')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def frozen_target(evidence):
    require(evidence["status"] == "PASS" and evidence["cleanup_completed"]
            and evidence["domain_state"] == "open" and not evidence["quarantined"]
            and not evidence["poisoned"] and evidence["pending_io"] == 0
            and not any(evidence["native_remaining"].values()), "D2a gate failed")
    proposal = evidence["proposed_d2b_target"]
    data = dict(proposal["target"])
    from dataclasses import asdict, fields
    target = T.TerminalExecutionTarget(**{f.name: data[f.name] for f in fields(T.TerminalExecutionTarget) if f.init})
    actual = json_projection(asdict(target))
    require(actual == data, "frozen target defaults differ")
    require(T.terminal_approval_arguments(target) == proposal["approval_arguments"], "frozen target projection differs")
    require(target.executable_requested == target.executable_resolved == PYTHON
            and target.executable_identity == "sha256:" + DIGEST
            and target.cwd == CWD and target.argv == ARGV, "unauthorized diagnostic target")
    require(proposal["expected_stdout"] == "D2b stdout\n"
            and proposal["expected_stderr"] == "D2b stderr\n"
            and proposal["expected_exit_code"] == 0, "unexpected D2a output contract")
    return target


def exact_capture(capture, expected):
    # utf8-replace/v1 preserves bytes/newlines; there is no normalization.
    require(capture.text == expected and capture.observed == capture.retained == len(expected.encode("utf-8"))
            and capture.eof and capture.complete and not capture.truncated
            and not capture.saturated, "unexpected or incomplete deterministic output")


def json_projection(value):
    import json
    return json.loads(json.dumps(value))

