"""Deterministic semantic backend: no OS calls, child, reader or timer thread."""
from collections import deque
from dataclasses import replace
from hashlib import sha256
from threading import Thread, current_thread

import pytest

from capabilities.terminal import TerminalExecutionTarget, _environment_identity
from capabilities.terminal_runtime import (
    FileEvidence, ReadResult, StdioResources, StreamResources,
    LaunchAdmissionDomain, WindowsTerminalRuntime,
)
from terminal_fixtures import console_pe_bytes


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, amount):
        self.now += amount


class Token:
    def __init__(self, name):
        self.name = name
        self.closed = False
        self.pending = False
        self.cancelled = False


def target_for(image=None, *, argv=()):
    image = console_pe_bytes() if image is None else image
    env = (("SYSTEMROOT", r"C:\Windows"), ("PATH", r"C:\Tools"))
    return TerminalExecutionTarget("tool.exe", r"C:\Tools\tool.exe",
                                   "sha256:" + sha256(image).hexdigest(), argv,
                                   r"C:\Work", env, _environment_identity(env))


def on_owner(function):
    values, errors = [], []

    def work():
        try:
            values.append(function(current_thread()))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=work, name="fake-action-owner")
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "owner failed to finish deterministically"
    if errors:
        raise errors[0]
    return values[0]


@pytest.fixture(autouse=True)
def forbid_live_loader(monkeypatch):
    import capabilities.terminal_win32 as native

    def forbidden(*args, **kwargs):
        raise AssertionError("D1b MUST NOT construct/load the live native backend")

    monkeypatch.setattr(native, "_load_live_bindings", forbidden)
    monkeypatch.setattr(native._LiveBindings, "__init__", forbidden)


class FakeRuntimeBackend:
    def __init__(self, clock, *, creation="P1", chunks=None):
        self.clock = clock
        self.creation = creation
        self.events = []
        self.created = False
        self.resumed = False
        self.exited = False
        self.active = 0
        self.exit = 0
        self.resume_count = 1
        self.member = True
        self.create_hook = None
        self.hooks = {}
        self.fail = set()
        self.release_fail = set()
        self.release_uncertain = set()
        self.cancel_unresolved = False
        self.keep_running = False
        self.keep_job = False
        self.descendants = False
        self.image = console_pe_bytes()
        self.position = 0
        self.executable_path = r"C:\Tools\tool.exe"
        self.cwd_path = r"C:\Work"
        self.executable_directory = False
        self.cwd_directory = True
        self.disk = True
        self.outputs = {s: deque(v) for s, v in (chunks or {
            "stdout": [b"out", None], "stderr": [b"err", None]}).items()}

    def _event(self, name, *args):
        self.events.append((name, *args))
        if name in self.hooks:
            self.hooks[name]()
        if name in self.fail:
            raise RuntimeError(name)

    def _own(self, scope, name, *, creation_only=False):
        token = Token(name)
        owner = scope.own(name, token, creation_only=creation_only)
        self._event("acquire", name)
        return owner, token

    def open_executable(self, scope, path):
        resource, self.exe = self._own(scope, "executable")
        self._event("open_executable", path)
        return resource

    def open_cwd(self, scope, path):
        resource, self.cwd = self._own(scope, "cwd")
        self._event("open_cwd", path)
        return resource

    def file_evidence(self, token):
        self._event("file_evidence", token.name)
        if token is self.exe:
            return FileEvidence(self.executable_path, len(self.image), self.executable_directory, self.disk, (1,))
        return FileEvidence(self.cwd_path, 0, self.cwd_directory, self.disk, (2,))

    def read_executable(self, token, size):
        self._event("read_executable", size)
        chunk = self.image[self.position:self.position + size]
        self.position += len(chunk)
        return chunk

    def create_job(self, scope):
        resource, self.job = self._own(scope, "job")
        self._event("create_job")
        return resource

    def configure_job(self, job):
        self._event("configure_job", job.name)

    def make_stdio(self, scope):
        self._event("make_stdio")
        children = []
        for name in ("stdin", "stdout", "stderr"):
            self._own(scope, name + "-original", creation_only=True)
            child, _ = self._own(scope, name + "-child", creation_only=True)
            children.append(child)
        for name in ("stdout", "stderr"):
            reader, _ = self._own(scope, name + "-reader")
            event, _ = self._own(scope, name + "-event")
            memory, token = self._own(scope, name + "-memory")
            token.stream = name
            scope.streams.append(StreamResources(name, reader, event, memory))
        return StdioResources(tuple(children))

    def connect_pipe(self, memory):
        self._event("connect_pipe", memory.stream)
        return ReadResult(True)

    def prepare_launch(self, scope, target, command_line, environment, job, children):
        self.target = target
        self.command = command_line
        self.environment = environment
        self._own(scope, "attributes", creation_only=True)
        resource, launch = self._own(scope, "launch", creation_only=True)
        launch.process, launch.thread = Token("root-process"), Token("primary-thread")
        self._event("prepare_launch")
        return resource

    def create_process(self, receipt, launch):
        if not receipt.begin_call():
            return
        self.events.append(("create_process",))
        if self.create_hook:
            self.create_hook()
        if self.creation == "CU":
            # Values are opaque even if a fake payload looks populated.
            launch.process.nonzero_looking = 0xE001
            launch.thread.nonzero_looking = 0xE002
            self.active = 1
            raise RuntimeError("native return unavailable")
        if self.creation == "C1":
            receipt.record_failed()
            return
        receipt.record_success()
        self.created = True
        self.active = 1
        self._event("after_success")
        receipt.attach_process(launch.process)
        receipt.attach_thread(launch.thread)

    def membership(self, process, job):
        self._event("membership", process.name, job.name)
        return self.member

    def resume(self, thread):
        self._event("resume", thread.name)
        self.resumed = self.resume_count == 1
        self.exited = not self.keep_running
        if self.exited:
            self.active = 1 if self.descendants else 0
        return self.resume_count

    def submit_read(self, memory):
        self._event("submit_read", memory.stream)
        memory.pending = True
        return ReadResult(False)

    def inspect_read(self, memory):
        self._event("inspect_read", memory.stream)
        if memory.cancelled:
            if self.cancel_unresolved:
                return ReadResult(False)
            memory.pending = False
            return ReadResult(True, error="read_cancelled")
        if not self.created and not self.exited:
            # Before creation, parent writers are still open.
            if not self.resumed:
                return ReadResult(False)
        queue = self.outputs[memory.stream]
        if not queue:
            return ReadResult(False)
        chunk = queue.popleft()
        memory.pending = False
        return ReadResult(True, eof=True) if chunk is None else ReadResult(True, chunk)

    def cancel_read(self, memory):
        self._event("cancel_read", memory.stream)
        memory.cancelled = True

    def root_exited(self, process):
        self._event("root_exited", process.name)
        return self.exited

    def exit_code(self, process):
        self._event("exit_code", process.name)
        return self.exit

    def job_active(self, job):
        self._event("job_active", job.name)
        return self.active

    def terminate_job(self, job):
        self._event("terminate_job", job.name)
        if not self.keep_job:
            self.active = 0
        self.exited = True

    def wait(self, process, memories, seconds):
        self._event("wait")
        self.clock.advance(max(seconds, 0.001))

    def release(self, token):
        self._event("release", token.name)
        if token.name in self.release_uncertain:
            raise KeyboardInterrupt()
        if token.name in self.release_fail or token.pending:
            return False
        token.closed = True
        return True


def run_fake(backend=None, *, domain=None, target=None):
    clock = FakeClock() if backend is None else backend.clock
    backend = FakeRuntimeBackend(clock) if backend is None else backend
    domain = LaunchAdmissionDomain(clock=clock) if domain is None else domain
    target = target_for() if target is None else target
    outcome = on_owner(lambda owner: WindowsTerminalRuntime(
        backend=backend, admission_domain=domain, owner_thread=owner).execute(target))
    return outcome, backend, domain
