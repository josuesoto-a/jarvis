import pytest

from core.contracts import (
    ActionRequest,
    ArgumentSource,
    CapabilitySpec,
    PermissionMode,
    RiskLevel,
)
from core.planner import (
    Planner,
    PlannerArgumentDraft,
    PlannerDraft,
    PlannerError,
    PlannerStepDraft,
)
from core.registry import (
    CapabilityRegistry,
)


# ============================================================
# FAKE OPENAI CLIENT
# ============================================================

class FakeResponse:

    def __init__(
        self,
        output_parsed,
    ):

        self.output_parsed = (
            output_parsed
        )


class FakeResponses:

    def __init__(
        self,
        output_parsed,
    ):

        self.output_parsed = (
            output_parsed
        )

        self.last_kwargs = None


    def parse(
        self,
        **kwargs,
    ):

        self.last_kwargs = kwargs

        return FakeResponse(
            self.output_parsed
        )


class FakeClient:

    def __init__(
        self,
        output_parsed,
    ):

        self.responses = FakeResponses(
            output_parsed
        )


# ============================================================
# HELPERS
# ============================================================

def build_registry() -> CapabilityRegistry:

    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="web_search",
            description=(
                "Search public internet information."
            ),
        )
    )

    registry.register(
        CapabilitySpec(
            name="browser",
            description=(
                "Open and navigate websites."
            ),
        )
    )

    return registry


def make_draft(
    *steps: PlannerStepDraft,
) -> PlannerDraft:

    return PlannerDraft(
        steps=list(
            steps
        )
    )


def literal_argument(
    name: str,
    value: str,
) -> PlannerArgumentDraft:

    return PlannerArgumentDraft(
        name=name,
        source=ArgumentSource.LITERAL,
        value=value,
        step_number=None,
        output_key=None,
    )


def step_output_argument(
    name: str,
    step_number: int,
    output_key: str,
) -> PlannerArgumentDraft:

    return PlannerArgumentDraft(
        name=name,
        source=ArgumentSource.STEP_OUTPUT,
        value=None,
        step_number=step_number,
        output_key=output_key,
    )


# ============================================================
# TESTS
# ============================================================

def test_capability_catalog_marks_availability():

    planner = Planner(
        build_registry(),
        client=FakeClient(
            None
        ),
    )

    catalog = {
        capability.name:
            capability
        for capability
        in planner.capability_catalog()
    }

    assert (
        catalog[
            "web_search"
        ].available
        is True
    )

    assert (
        catalog[
            "browser"
        ].available
        is True
    )

    assert (
        catalog[
            "computer_use"
        ].available
        is False
    )


def test_registered_custom_capability_enters_catalog():

    registry = (
        build_registry()
    )

    registry.register(
        CapabilitySpec(
            name="music",
            description=(
                "Control music playback."
            ),
        )
    )

    planner = Planner(
        registry,
        client=FakeClient(
            None
        ),
    )

    names = tuple(
        capability.name
        for capability
        in planner.capability_catalog()
    )

    assert "music" in names


def test_draft_becomes_execution_plan():

    request = ActionRequest(
        goal=(
            "Find a product and "
            "open its page"
        ),
        raw_input=(
            "Busca el producto y ábrelo."
        ),
    )

    draft = make_draft(
        PlannerStepDraft(
            description=(
                "Search for the product"
            ),
            capability="web_search",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        ),
        PlannerStepDraft(
            description=(
                "Open the selected result"
            ),
            capability="browser",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        ),
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    plan = planner.plan(
        request
    )

    assert (
        plan.request_id
        == request.request_id
    )

    assert tuple(
        step.step_number
        for step in plan.steps
    ) == (
        1,
        2,
    )

    assert tuple(
        step.capability
        for step in plan.steps
    ) == (
        "web_search",
        "browser",
    )


def test_missing_capability_can_appear_in_plan():

    request = ActionRequest(
        goal=(
            "Find a product and "
            "operate its GUI"
        ),
        raw_input=(
            "Busca esto y haz clic."
        ),
    )

    draft = make_draft(
        PlannerStepDraft(
            description=(
                "Search for the product"
            ),
            capability="web_search",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        ),
        PlannerStepDraft(
            description=(
                "Interact with the "
                "graphical interface"
            ),
            capability="computer_use",
            arguments=[],
            risk=RiskLevel.MEDIUM,
            permission=(
                PermissionMode
                .CONFIRM_BEFORE_EXECUTION
            ),
        ),
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    plan = planner.plan(
        request
    )

    assert (
        plan.steps[
            1
        ].capability
        == "computer_use"
    )


def test_unknown_capability_is_rejected():

    request = ActionRequest(
        goal="Do something",
        raw_input="Haz algo",
    )

    draft = make_draft(
        PlannerStepDraft(
            description=(
                "Use magical capability"
            ),
            capability="telepathy",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    with pytest.raises(
        PlannerError
    ):

        planner.plan(
            request
        )


def test_overall_risk_is_computed_locally():

    request = ActionRequest(
        goal="Research and interact",
        raw_input="Investiga y haz clic.",
    )

    draft = make_draft(
        PlannerStepDraft(
            description="Research",
            capability="web_search",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        ),
        PlannerStepDraft(
            description="Interact",
            capability="computer_use",
            arguments=[],
            risk=RiskLevel.HIGH,
            permission=(
                PermissionMode
                .CONFIRM_BEFORE_EXECUTION
            ),
        ),
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    plan = planner.plan(
        request
    )

    assert (
        plan.overall_risk
        == RiskLevel.HIGH
    )


def test_responses_api_receives_structured_schema():

    request = ActionRequest(
        goal="Search the web",
        raw_input="Busca esto.",
    )

    draft = make_draft(
        PlannerStepDraft(
            description="Search",
            capability="web_search",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )

    fake_client = FakeClient(
        draft
    )

    planner = Planner(
        build_registry(),
        client=fake_client,
        model="test-model",
    )

    planner.plan(
        request
    )

    kwargs = (
        fake_client
        .responses
        .last_kwargs
    )

    assert (
        kwargs["model"]
        == "test-model"
    )

    assert (
        kwargs["text_format"]
        is PlannerDraft
    )

    assert (
        "KNOWN CAPABILITIES:"
        in kwargs["input"]
    )


def test_missing_structured_result_is_rejected():

    planner = Planner(
        build_registry(),
        client=FakeClient(
            None
        ),
    )

    request = ActionRequest(
        goal="Search",
        raw_input="Busca",
    )

    with pytest.raises(
        PlannerError
    ):

        planner.plan(
            request
        )


def test_literal_step_argument_is_preserved():

    request = ActionRequest(
        goal="Search Big Mac price",
        raw_input=(
            "Busca cuánto cuesta "
            "una Big Mac."
        ),
    )

    draft = make_draft(
        PlannerStepDraft(
            description=(
                "Search current Big Mac price"
            ),
            capability="web_search",
            arguments=[
                literal_argument(
                    "query",
                    "current Big Mac price Costa Rica",
                )
            ],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    plan = planner.plan(
        request
    )

    argument = (
        plan.steps[
            0
        ].arguments[
            "query"
        ]
    )

    assert (
        argument.source
        == ArgumentSource.LITERAL
    )

    assert (
        argument.value
        == (
            "current Big Mac price Costa Rica"
        )
    )


def test_step_arguments_can_be_empty():

    request = ActionRequest(
        goal="Open browser",
        raw_input=(
            "Abre el navegador."
        ),
    )

    draft = make_draft(
        PlannerStepDraft(
            description="Open browser",
            capability="browser",
            arguments=[],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    plan = planner.plan(
        request
    )

    assert (
        plan.steps[
            0
        ].arguments
        == {}
    )


def test_step_output_argument_is_preserved():

    request = ActionRequest(
        goal=(
            "Search for something "
            "and open it"
        ),
        raw_input=(
            "Busca Python y abre "
            "el resultado."
        ),
    )

    draft = make_draft(
        PlannerStepDraft(
            description=(
                "Search for Python"
            ),
            capability="web_search",
            arguments=[
                literal_argument(
                    "query",
                    "Python programming language",
                )
            ],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        ),
        PlannerStepDraft(
            description=(
                "Open the selected result"
            ),
            capability="browser",
            arguments=[
                step_output_argument(
                    name="url",
                    step_number=1,
                    output_key="best_result_url",
                )
            ],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        ),
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    plan = planner.plan(
        request
    )

    first_argument = (
        plan.steps[
            0
        ].arguments[
            "query"
        ]
    )

    second_argument = (
        plan.steps[
            1
        ].arguments[
            "url"
        ]
    )

    assert (
        first_argument.source
        == ArgumentSource.LITERAL
    )

    assert (
        second_argument.source
        == ArgumentSource.STEP_OUTPUT
    )

    assert (
        second_argument.step_number
        == 1
    )

    assert (
        second_argument.output_key
        == "best_result_url"
    )


def test_structured_output_schema_requires_all_argument_fields():
    schema = (
        PlannerDraft
        .model_json_schema()
    )

    step_schema = (
        schema[
            "$defs"
        ][
            "PlannerStepDraft"
        ]
    )

    argument_schema = (
        schema[
            "$defs"
        ][
            "PlannerArgumentDraft"
        ]
    )

    assert (
        step_schema[
            "additionalProperties"
        ]
        is False
    )

    assert (
        argument_schema[
            "additionalProperties"
        ]
        is False
    )

    assert (
        "arguments"
        in step_schema[
            "required"
        ]
    )

    assert set(
        argument_schema[
            "required"
        ]
    ) == {
        "name",
        "source",
        "value",
        "step_number",
        "output_key",
    }


def test_duplicate_argument_names_are_rejected():

    request = ActionRequest(
        goal="Search",
        raw_input="Busca algo.",
    )

    draft = make_draft(
        PlannerStepDraft(
            description="Search",
            capability="web_search",
            arguments=[
                literal_argument(
                    "query",
                    "first query",
                ),
                literal_argument(
                    "query",
                    "second query",
                ),
            ],
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )

    planner = Planner(
        build_registry(),
        client=FakeClient(
            draft
        ),
    )

    with pytest.raises(
        PlannerError
    ):
        planner.plan(
            request
        )
