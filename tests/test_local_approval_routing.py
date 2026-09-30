"""D1F-B4: stored capability routes presentation without granting authority."""

import ast
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

import capabilities.browser as browser_module
import core.executor as executor_module
from core.action_worker import ActionWorker
from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import PermissionMode, RiskLevel
from core.orchestrator import Orchestrator
from integrations.local_approval import LocalApproval
from integrations.local_approval_defaults import build_default_local_approval_registry
from integrations.local_approval_registry import (
    InvalidLocalApprovalAdapterError, LocalApprovalAdapter,
    LocalApprovalAdapterNotFoundError, LocalApprovalAdapterRegistry,
)
from integrations.local_browser_approval import (
    preview_browser_target, preview_browser_approval,
    preview_checkpoint_browser_approval, preview_browser_approval_v2,
)
from integrations.openai_live import PendingPermissionUpdate

# Reuse B3's real engine/runtime harness so routing tests cover the same path.
from test_browser_pending_integration import (
    URL, claim, projection as engine_projection, replacement,
    replace_pending_target, session, system, target as engine_target,
)


def make_target(*, request_id=None, step_number=1, capability="browser",
                risk=RiskLevel.LOW, arguments=None, prepared_target=URL):
    return PendingApprovalTarget(
        subject=ApprovalSubject(
            request_id=request_id or uuid4(), step_number=step_number,
            capability=capability, risk=risk,
            arguments={"url": prepared_target} if arguments is None else arguments,
        ),
        effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
        prepared_target=prepared_target,
    )


def forbidden(*args, **kwargs):
    pytest.fail("Unrelated adapter or target discovery was invoked")


def unrelated_adapter():
    # This key sorts before browser, detecting the old probing algorithm.
    return LocalApprovalAdapter("aardvark", forbidden, forbidden, forbidden)


@pytest.fixture
def routing():
    stored = make_target()
    pending = PendingPermissionUpdate(
        delegation_id="delegation-1", call_id="call-1",
        request_id=str(stored.subject.request_id), confirmation_steps=(1,),
        pending_confirmation_steps=(1,), message="Confirm locally",
    )
    state = SimpleNamespace(target=stored, projection=pending, reads=[])

    def read(request_id, *, step_number):
        state.reads.append((request_id, step_number))
        return state.target

    state.engine = SimpleNamespace(preview_pending_approval=read)
    state.registry = build_default_local_approval_registry()
    state.registry.register(unrelated_adapter())
    return state


def test_preview_reads_once_and_passes_same_target_to_only_selected_adapter(routing):
    calls = []
    registry = LocalApprovalAdapterRegistry()

    def preview(projection, pending_target):
        calls.append((projection, pending_target))
        return preview_browser_target(projection, pending_target)

    registry.register(LocalApprovalAdapter("browser", preview, forbidden, forbidden))
    registry.register(unrelated_adapter())
    registry.names = forbidden  # Resolution must not enumerate registry keys.
    approval = registry.resolve(routing.engine, routing.projection)
    assert calls == [(routing.projection, routing.target)]
    assert calls[0][1] is routing.target
    assert approval.subject is routing.target.subject
    assert approval.call_id == routing.projection.call_id
    assert routing.reads == [(routing.target.subject.request_id, 1)]


def test_selected_decline_does_not_probe_or_fall_back(routing):
    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter("browser", lambda p, t: None, forbidden, forbidden))
    registry.register(unrelated_adapter())
    assert registry.resolve(routing.engine, routing.projection) is None
    assert len(routing.reads) == 1


@pytest.mark.parametrize("missing", ["target", "adapter"])
def test_missing_target_or_unknown_stored_capability_returns_none(routing, missing):
    routing.target = None if missing == "target" else make_target(
        request_id=routing.target.subject.request_id, capability="future_capability",
    )
    assert routing.registry.resolve(routing.engine, routing.projection) is None
    assert len(routing.reads) == 1


@pytest.mark.parametrize("changes", [
    {"request_id": "bad-uuid"}, {"request_id": None}, {"request_id": 1},
    {"pending_confirmation_steps": ()}, {"pending_confirmation_steps": (1, 2)},
    {"pending_confirmation_steps": ("1",)}, {"pending_confirmation_steps": (True,)},
    {"pending_confirmation_steps": (1.0,)}, {"pending_confirmation_steps": (0,)},
    {"pending_confirmation_steps": (-1,)}, {"confirmation_steps": ()},
    {"confirmation_steps": (2,)},
])
def test_bad_projection_is_rejected_before_stored_read(routing, changes):
    projection = replace(routing.projection, **changes)
    assert routing.registry.resolve(routing.engine, projection) is None
    assert routing.reads == []


@pytest.mark.parametrize("mismatch", ["request", "step", "type"])
def test_mismatched_stored_target_never_reaches_adapter(routing, mismatch):
    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter("browser", forbidden, forbidden, forbidden))
    if mismatch == "request":
        routing.target = make_target()
    elif mismatch == "step":
        routing.target = make_target(request_id=routing.target.subject.request_id, step_number=2)
    else:
        routing.target = object()
    assert registry.resolve(routing.engine, routing.projection) is None
    assert len(routing.reads) == 1


@pytest.mark.parametrize("invalid", [
    "type", "call", "equal_subject_copy", "request", "step", "url", "capability",
])
def test_adapter_output_must_retain_exact_subject_and_call(routing, invalid):
    subject = routing.target.subject
    call_id = routing.projection.call_id
    if invalid == "call":
        call_id = "another-call"
    elif invalid not in {"type", "call"}:
        metadata = dict(request_id=subject.request_id, step_number=subject.step_number,
                        capability=subject.capability, risk=subject.risk,
                        arguments=subject.arguments)
        if invalid == "request":
            metadata["request_id"] = uuid4()
        elif invalid == "step":
            metadata["step_number"] = 2
        elif invalid == "url":
            metadata["arguments"] = {"url": "https://example.com/other"}
        elif invalid == "capability":
            metadata["capability"] = "future_capability"
        subject = ApprovalSubject(**metadata)
        if invalid == "equal_subject_copy":
            assert subject.same_target_as(routing.target.subject)
            assert subject is not routing.target.subject
    result = object() if invalid == "type" else LocalApproval(call_id=call_id, subject=subject)
    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter("browser", lambda p, t: result, forbidden, forbidden))
    with pytest.raises(InvalidLocalApprovalAdapterError):
        registry.resolve(routing.engine, routing.projection)


def test_lookup_normalization_never_rewrites_stored_subject(routing):
    routing.target = make_target(request_id=routing.target.subject.request_id, capability="BROWSER")
    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter(
        "browser", lambda p, t: LocalApproval(call_id=p.call_id, subject=t.subject),
        forbidden, forbidden,
    ))
    with pytest.raises(InvalidLocalApprovalAdapterError):
        registry.resolve(routing.engine, routing.projection)
    assert routing.target.subject.capability == "BROWSER"


def test_browser_preview_consumes_target_without_fetch_preparation_or_subject_creation(routing, monkeypatch):
    monkeypatch.setattr(Orchestrator, "preview_pending_approval", forbidden)
    monkeypatch.setattr(browser_module, "prepare_browser_approval", forbidden)
    monkeypatch.setattr(browser_module, "ApprovalSubject", forbidden)
    monkeypatch.setattr(executor_module, "prepare_browser_approval", forbidden)
    approval = preview_browser_target(routing.projection, routing.target)
    assert approval.subject is routing.target.subject
    assert approval.url == URL
    assert routing.reads == []


@pytest.mark.parametrize("invalid", ["capability", "risk", "target_type", "arguments", "permission"])
def test_browser_preview_preserves_supported_shape(routing, invalid):
    options = dict(request_id=routing.target.subject.request_id)
    if invalid == "capability":
        options["capability"] = "future_capability"
    elif invalid == "risk":
        options["risk"] = RiskLevel.HIGH
    elif invalid == "target_type":
        options["prepared_target"] = [URL]
    elif invalid == "arguments":
        options["arguments"] = {"url": URL, "extra": True}
    target = make_target(**options)
    if invalid == "permission":
        # Simulate corrupted external input; normal envelope construction rejects it.
        object.__setattr__(target, "effective_permission", PermissionMode.AUTOMATIC)
    assert preview_browser_target(routing.projection, target) is None


def test_render_uses_retained_subject_without_pending_lookup(routing):
    approval = routing.registry.resolve(routing.engine, routing.projection)
    routing.target = None
    routing.engine.preview_pending_approval = forbidden
    assert "URL EXACTA: " + URL in routing.registry.render(approval)
    assert len(routing.reads) == 1
    with pytest.raises(LocalApprovalAdapterNotFoundError):
        LocalApprovalAdapterRegistry().render(approval)


def test_missing_freshness_adapter_returns_false(routing):
    approval = routing.registry.resolve(routing.engine, routing.projection)
    routing.registry._adapters.pop("browser")
    assert not routing.registry.is_current(routing.engine, object(), approval)
    assert len(routing.reads) == 1


@pytest.mark.parametrize("error_type", [RuntimeError, LocalApprovalAdapterNotFoundError])
def test_freshness_does_not_swallow_adapter_errors(routing, error_type):
    approval = routing.registry.resolve(routing.engine, routing.projection)

    def broken(*args):
        raise error_type("adapter bug")

    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter("browser", forbidden, forbidden, broken))
    with pytest.raises(error_type, match="adapter bug"):
        registry.is_current(routing.engine, object(), approval)


@pytest.mark.parametrize("wrapper", [
    preview_browser_approval, preview_checkpoint_browser_approval, preview_browser_approval_v2,
])
def test_compatibility_wrappers_only_read_stored_target(routing, wrapper, monkeypatch):
    routing.engine.preview_single_browser = forbidden
    routing.engine.preview_checkpoint_browser = forbidden
    monkeypatch.setattr(browser_module, "ApprovalSubject", forbidden)
    monkeypatch.setattr(executor_module, "prepare_browser_approval", forbidden)
    approval = wrapper(routing.engine, routing.projection)
    assert approval.subject is routing.target.subject
    assert len(routing.reads) == 1


@pytest.mark.parametrize("dependency", [False, True])
def test_registry_preview_grants_no_authority_and_exact_claim_dispatches(dependency, monkeypatch):
    setup = system(dependency=dependency)
    prepared = []
    prepare = executor_module.prepare_browser_approval

    def record_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(executor_module, "prepare_browser_approval", record_prepare)
    setup.engine.run(setup.request)
    stored = engine_target(setup)
    reads, previews = [], []
    read = setup.engine.preview_pending_approval

    def record_read(*args, **kwargs):
        reads.append((args, kwargs))
        return read(*args, **kwargs)

    registry = build_default_local_approval_registry()
    selected = registry.get("browser")

    def record_preview(projection, target):
        previews.append(target)
        return selected.preview(projection, target)

    registry._adapters["browser"] = replace(selected, preview=record_preview)
    registry.register(unrelated_adapter())
    monkeypatch.setattr(setup.engine, "preview_pending_approval", record_read)
    monkeypatch.setattr(setup.engine, "preview_single_browser", forbidden)
    monkeypatch.setattr(setup.engine, "preview_checkpoint_browser", forbidden)
    approval = registry.resolve(setup.engine, engine_projection(setup))
    assert len(reads) == 1
    assert previews == [stored] and previews[0] is stored
    assert approval.subject is stored.subject
    assert URL in registry.render(approval)
    assert setup.opened == setup.dispatched == []
    assert engine_target(setup) is stored  # Preview did not consume pending.
    assert registry.is_current(setup.engine, session(setup), approval)
    assert setup.opened == []
    continued = []
    execute = setup.executor.execute

    def record_execution(plan, **kwargs):
        continued.append(kwargs["pending_approval_target"])
        return execute(plan, **kwargs)

    monkeypatch.setattr(setup.executor, "execute", record_execution)
    assert claim(setup, approval.subject).completed
    assert continued[0] is stored
    assert setup.dispatched == [{"url": stored.prepared_target}]
    assert setup.opened == [stored.prepared_target]
    assert len(prepared) == 1
    assert len(setup.searched) == int(dependency)


@pytest.mark.parametrize("change", ["browser", "capability", "gone"])
def test_stale_approval_routes_by_retained_capability_and_cannot_claim(change):
    setup = system()
    setup.engine.run(setup.request)
    registry = build_default_local_approval_registry()
    approval = registry.resolve(setup.engine, engine_projection(setup))
    registry.register(LocalApprovalAdapter("future_capability", forbidden, forbidden, forbidden))
    if change == "gone":
        with setup.engine._lock:
            del setup.engine._pending[setup.request.request_id]
    else:
        changed = replacement(setup) if change == "browser" else make_target(
            request_id=setup.request.request_id, capability="future_capability",
        )
        replace_pending_target(setup, changed)
    assert URL in registry.render(approval)
    assert not registry.is_current(setup.engine, session(setup), approval)
    if change == "gone":
        assert not claim(setup, approval.subject).completed
    else:
        with pytest.raises(ValueError, match="current pending target"):
            claim(setup, approval.subject)
    assert setup.opened == []


@pytest.mark.parametrize("stale", ["call", "request", "step", "missing_action"])
def test_registry_preserves_session_freshness(stale):
    setup = system()
    setup.engine.run(setup.request)
    registry = build_default_local_approval_registry()
    registry.register(unrelated_adapter())
    approval = registry.resolve(setup.engine, engine_projection(setup))
    options = {}
    if stale == "call":
        options["call_id"] = "stale-call"
    elif stale == "request":
        options["request_id"] = str(uuid4())
    elif stale == "step":
        options["step"] = 2
    action_session = session(setup, **options)
    if stale == "missing_action":
        action_session = SimpleNamespace(snapshot=lambda: SimpleNamespace(action=None))
    assert not registry.is_current(setup.engine, action_session, approval)
    assert setup.opened == []


def test_change_after_freshness_still_rejected_by_atomic_claim():
    setup = system()
    setup.engine.run(setup.request)
    registry = build_default_local_approval_registry()
    approval = registry.resolve(setup.engine, engine_projection(setup))
    assert registry.is_current(setup.engine, session(setup), approval)
    changed = replace_pending_target(setup, replacement(setup))
    with pytest.raises(ValueError, match="current pending target"):
        claim(setup, approval.subject)
    assert setup.engine._pending[setup.request.request_id] is changed
    assert setup.opened == []


def test_change_after_worker_admission_still_rejected_by_atomic_claim(monkeypatch):
    setup = system()
    worker = ActionWorker(setup.engine.run, capacity=2)
    entered, release = Event(), Event()
    resume = setup.engine.resume

    def gated(request_id, **kwargs):
        entered.set()
        assert release.wait(5)
        return resume(request_id, **kwargs)

    monkeypatch.setattr(worker, "_resume", gated)
    worker.start()
    try:
        request_id = worker.submit(dict(goal=setup.request.goal, raw_input=setup.request.raw_input,
                                        request_id=str(setup.request.request_id)))
        assert worker.result(request_id, timeout=5)["status"] == "waiting_for_permission"
        registry = build_default_local_approval_registry()
        approval = registry.resolve(setup.engine, engine_projection(setup))
        worker.confirm(request_id, confirmed_steps=frozenset({1}), expected_subject=approval.subject)
        assert entered.wait(5)
        changed = replace_pending_target(setup, replacement(setup))
        release.set()
        assert worker.result(request_id, timeout=5)["status"] == "failed"
        assert setup.engine._pending[setup.request.request_id] is changed
        assert setup.opened == []
    finally:
        release.set()
        worker.shutdown(wait=True)


def test_production_resolve_has_no_probing_loop_and_browser_preview_has_no_discovery():
    source_root = Path(__file__).parents[1]
    registry_tree = ast.parse((source_root / "integrations/local_approval_registry.py").read_text(encoding="utf-8"))
    resolve = next(node for node in ast.walk(registry_tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "resolve")
    assert not any(isinstance(node, (ast.For, ast.AsyncFor, ast.While,
                                    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
                   for node in ast.walk(resolve))
    browser_tree = ast.parse((source_root / "integrations/local_browser_approval.py").read_text(encoding="utf-8"))
    preview = next(node for node in ast.walk(browser_tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "preview_browser_target")
    assert [argument.arg for argument in preview.args.args] == ["projection", "pending_target"]
    prohibited = {"preview_pending_approval", "prepare_browser_approval", "ApprovalSubject",
                  "preview_single_browser", "preview_checkpoint_browser", "_resolve_arguments"}
    assert not any(isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Name) and node.func.id in prohibited
        or isinstance(node.func, ast.Attribute) and node.func.attr in prohibited
    ) for node in ast.walk(preview))
