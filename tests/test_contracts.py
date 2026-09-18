from core.contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    CapabilityRequirement,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)


def test_action_request_has_unique_id():
    first = ActionRequest(
        goal="Consultar clima",
        raw_input="¿Cómo está el clima?",
    )

    second = ActionRequest(
        goal="Consultar clima",
        raw_input="¿Cómo está el clima?",
    )

    assert first.request_id != second.request_id


def test_capability_requirement():
    requirement = CapabilityRequirement(
        capability="weather",
        reason="Necesitamos información meteorológica actual.",
    )

    assert requirement.capability == "weather"
    assert requirement.required is True


def test_execution_plan():
    request = ActionRequest(
        goal="Consultar clima en Cartago",
        raw_input="¿Cómo está el clima en Cartago?",
    )

    step = ExecutionStep(
        step_number=1,
        description="Consultar proveedor meteorológico",
        capability="weather",
        risk=RiskLevel.LOW,
        permission=PermissionMode.AUTOMATIC,
    )

    plan = ExecutionPlan(
        request_id=request.request_id,
        steps=(step,),
        overall_risk=RiskLevel.LOW,
    )

    assert plan.request_id == request.request_id
    assert len(plan.steps) == 1
    assert plan.steps[0].capability == "weather"


def test_action_result():
    request = ActionRequest(
        goal="Consultar clima",
        raw_input="Consulta el clima",
    )

    result = ActionResult(
        request_id=request.request_id,
        status=ActionStatus.COMPLETED,
        summary="Consulta completada.",
        data={
            "temperature_c": 22.5,
        },
    )

    assert result.status == ActionStatus.COMPLETED
    assert result.data["temperature_c"] == 22.5
