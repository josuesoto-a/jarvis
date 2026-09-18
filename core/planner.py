"""
LLM-backed planning for Jarvis.

The planner converts an ActionRequest into a structured ExecutionPlan.

Important boundaries:

- The planner does not execute actions.
- The planner may only use known capability IDs.
- Missing capabilities may still appear when genuinely required.
- Risk and permission values are proposals only.
- Arguments explicitly distinguish literal values from outputs
  produced by previous steps.
- The Structured Outputs schema intentionally uses arrays of typed
  argument objects instead of arbitrary dictionaries.
"""

from dataclasses import dataclass

from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from core.contracts import (
    ActionRequest,
    ArgumentSource,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.registry import (
    CapabilityRegistry,
    canonicalize_capability_name,
)


# ============================================================
# ERRORS
# ============================================================

class PlannerError(Exception):
    """
    Raised when a planning response cannot safely become
    an ExecutionPlan.
    """


# ============================================================
# KNOWN PRIMITIVE CAPABILITIES
# ============================================================

DEFAULT_CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    "weather": (
        "Read current or forecast weather information."
    ),
    "web_search": (
        "Search public internet information."
    ),
    "browser": (
        "Open and navigate websites."
    ),
    "computer_use": (
        "Interact with graphical interfaces using screen, "
        "mouse, and keyboard."
    ),
    "filesystem": (
        "Read, create, move, or modify local files."
    ),
    "terminal": (
        "Execute local command-line operations."
    ),
    "codex": (
        "Delegate software engineering work to a coding agent."
    ),
}


# ============================================================
# CAPABILITY VIEW
# ============================================================

@dataclass(frozen=True, slots=True)
class PlannerCapability:
    """
    Capability information presented to the planner.
    """

    name: str
    description: str
    available: bool


# ============================================================
# STRUCTURED ARGUMENT
# ============================================================

class PlannerArgumentDraft(BaseModel):
    """
    One proposed capability argument.

    IMPORTANT:

    Every field is required in the JSON Schema.

    Fields that do not apply use null instead of being omitted.

    Literal example:

        {
            "name": "query",
            "source": "literal",
            "value": "Big Mac price Costa Rica",
            "step_number": null,
            "output_key": null
        }

    Step-output example:

        {
            "name": "url",
            "source": "step_output",
            "value": null,
            "step_number": 1,
            "output_key": "best_result_url"
        }
    """

    model_config = ConfigDict(
        extra="forbid"
    )

    name: str = Field(
        min_length=1,
        max_length=100,
    )

    source: ArgumentSource

    value: str | None

    step_number: int | None

    output_key: str | None


# ============================================================
# STRUCTURED STEP
# ============================================================

class PlannerStepDraft(BaseModel):
    """
    One LLM-proposed execution step.

    Step numbers are assigned locally by Jarvis.

    arguments is always present.
    Use [] when the step requires no arguments.
    """

    model_config = ConfigDict(
        extra="forbid"
    )

    description: str = Field(
        min_length=1,
        max_length=500,
    )

    capability: str = Field(
        min_length=1,
        max_length=100,
    )

    arguments: list[
        PlannerArgumentDraft
    ]

    risk: RiskLevel

    permission: PermissionMode


# ============================================================
# STRUCTURED PLAN
# ============================================================

class PlannerDraft(BaseModel):
    """
    Structured planning result returned by the model.
    """

    model_config = ConfigDict(
        extra="forbid"
    )

    steps: list[
        PlannerStepDraft
    ] = Field(
        min_length=1,
        max_length=50,
    )


# ============================================================
# RISK ORDER
# ============================================================

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def _maximum_risk(
    risks: tuple[RiskLevel, ...],
) -> RiskLevel:
    """
    Compute overall risk locally.

    The model never controls overall_risk directly.
    """

    if not risks:
        raise PlannerError(
            "A plan must contain at least one "
            "risk-bearing step."
        )

    return max(
        risks,
        key=lambda risk: _RISK_ORDER[risk],
    )


# ============================================================
# PLANNER INSTRUCTIONS
# ============================================================

PLANNER_INSTRUCTIONS = """
You are the planning component of Jarvis.

Your only job is to transform one user goal into a minimal,
ordered execution plan.

You DO NOT execute the plan.

Rules:

1. Use only capability IDs explicitly listed in the input.

2. Never invent a capability ID.

3. A capability marked unavailable may still appear when the goal
   genuinely requires it. Another component will block execution.

4. Do not omit required steps merely because a capability is unavailable.

5. Use the smallest complete set of steps necessary to accomplish the goal.

6. Each step must represent one meaningful external action or delegation.

7. Preserve dependency order.

8. Every step MUST include an "arguments" array.
   Use an empty array when the capability needs no arguments.

9. Every argument MUST contain all five fields:
       name
       source
       value
       step_number
       output_key

10. Use source="literal" only when the value is already known when
    the plan is created.

11. A literal argument MUST use:
        name = machine-oriented argument name
        source = "literal"
        value = actual execution value
        step_number = null
        output_key = null

12. When an argument cannot exist until an earlier step executes,
    use source="step_output".

13. A step-output argument MUST use:
        name = machine-oriented argument name
        source = "step_output"
        value = null
        step_number = earlier step that produces the data
        output_key = short machine-oriented name of the required output

14. Never put human placeholder text inside a literal argument.

    BAD:
        name = "url"
        source = "literal"
        value = "the relevant result from the previous search"

    GOOD:
        name = "url"
        source = "step_output"
        value = null
        step_number = 1
        output_key = "best_result_url"

15. Use short explicit argument names such as:
        query
        url
        location
        path
        command
        task

16. Arguments contain execution data, not explanations.

17. Risk and permission values are provisional proposals only.

18. Research/read-only actions are normally lower risk than actions
    that modify state.

19. Financial transactions, destructive operations, credential changes,
    consequential communications, and irreversible actions must not be
    marked automatic.

20. Do not add conversational filler as execution steps.

21. Do not claim any action has already happened.

Return only the structured planning result requested by the schema.
"""


# ============================================================
# PLANNER
# ============================================================

class Planner:
    """
    Convert ActionRequest objects into ExecutionPlan objects.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        client: OpenAI | None = None,
        model: str = "gpt-5.6-luna",
    ) -> None:

        self._registry = registry

        self._client = (
            client
            if client is not None
            else OpenAI()
        )

        self._model = model


    # --------------------------------------------------------
    # CAPABILITY CATALOG
    # --------------------------------------------------------

    def capability_catalog(
        self,
    ) -> tuple[PlannerCapability, ...]:

        descriptions = dict(
            DEFAULT_CAPABILITY_DESCRIPTIONS
        )

        for spec in self._registry.list_capabilities():
            descriptions[
                spec.name
            ] = spec.description

        capabilities: list[
            PlannerCapability
        ] = []

        for name in sorted(descriptions):

            if self._registry.has(name):

                spec = self._registry.get(
                    name
                )

                description = (
                    spec.description
                )

                available = True

            else:

                description = (
                    descriptions[name]
                )

                available = False

            capabilities.append(
                PlannerCapability(
                    name=name,
                    description=description,
                    available=available,
                )
            )

        return tuple(
            capabilities
        )


    # --------------------------------------------------------
    # INPUT
    # --------------------------------------------------------

    def _build_input(
        self,
        request: ActionRequest,
    ) -> str:

        lines = [
            "USER GOAL:",
            request.goal,
            "",
            "ORIGINAL USER INPUT:",
            request.raw_input,
            "",
            "KNOWN CAPABILITIES:",
        ]

        for capability in self.capability_catalog():

            availability = (
                "AVAILABLE"
                if capability.available
                else "UNAVAILABLE"
            )

            lines.append(
                f"- {capability.name} "
                f"[{availability}]: "
                f"{capability.description}"
            )

        if request.context:

            lines.extend(
                [
                    "",
                    "CONTEXT:",
                    repr(
                        dict(
                            request.context
                        )
                    ),
                ]
            )

        return "\n".join(
            lines
        )


    # --------------------------------------------------------
    # ARGUMENT CONVERSION
    # --------------------------------------------------------

    def _convert_argument(
        self,
        draft: PlannerArgumentDraft,
    ) -> tuple[
        str,
        ExecutionArgument,
    ]:

        argument_name = (
            draft.name.strip()
        )

        if not argument_name:
            raise PlannerError(
                "Planner returned an empty argument name."
            )


        # ----------------------------------------------------
        # LITERAL
        # ----------------------------------------------------

        if (
            draft.source
            == ArgumentSource.LITERAL
        ):

            if (
                draft.value is None
                or not draft.value.strip()
            ):
                raise PlannerError(
                    f"Literal argument "
                    f"{argument_name!r} "
                    "has no value."
                )

            if (
                draft.step_number is not None
                or draft.output_key is not None
            ):
                raise PlannerError(
                    f"Literal argument "
                    f"{argument_name!r} "
                    "must not reference a step output."
                )

            try:

                argument = (
                    ExecutionArgument.literal(
                        draft.value
                    )
                )

            except ValueError as error:

                raise PlannerError(
                    f"Invalid argument "
                    f"{argument_name!r}: "
                    f"{error}"
                ) from error

            return (
                argument_name,
                argument,
            )


        # ----------------------------------------------------
        # STEP OUTPUT
        # ----------------------------------------------------

        if (
            draft.source
            == ArgumentSource.STEP_OUTPUT
        ):

            if draft.value is not None:

                raise PlannerError(
                    f"Step-output argument "
                    f"{argument_name!r} "
                    "must not contain a literal value."
                )

            if (
                draft.step_number is None
                or draft.output_key is None
                or not draft.output_key.strip()
            ):
                raise PlannerError(
                    f"Step-output argument "
                    f"{argument_name!r} "
                    "is incomplete."
                )

            try:

                argument = (
                    ExecutionArgument.step_output(
                        step_number=(
                            draft.step_number
                        ),
                        output_key=(
                            draft.output_key
                        ),
                    )
                )

            except ValueError as error:

                raise PlannerError(
                    f"Invalid argument "
                    f"{argument_name!r}: "
                    f"{error}"
                ) from error

            return (
                argument_name,
                argument,
            )


        raise PlannerError(
            f"Unsupported argument source for "
            f"{argument_name!r}: "
            f"{draft.source}"
        )


    # --------------------------------------------------------
    # DRAFT -> PLAN
    # --------------------------------------------------------

    def _draft_to_plan(
        self,
        request: ActionRequest,
        draft: PlannerDraft,
    ) -> ExecutionPlan:

        known_names = {
            capability.name
            for capability
            in self.capability_catalog()
        }

        steps: list[
            ExecutionStep
        ] = []

        for number, draft_step in enumerate(
            draft.steps,
            start=1,
        ):

            try:

                capability = (
                    canonicalize_capability_name(
                        draft_step.capability
                    )
                )

            except Exception as error:

                raise PlannerError(
                    "Planner returned an invalid "
                    f"capability ID: "
                    f"{draft_step.capability!r}"
                ) from error


            if capability not in known_names:

                raise PlannerError(
                    "Planner returned an unknown "
                    f"capability: {capability}"
                )


            description = (
                draft_step.description.strip()
            )

            if not description:

                raise PlannerError(
                    "Planner returned an empty "
                    "step description."
                )


            arguments: dict[
                str,
                ExecutionArgument,
            ] = {}


            for argument_draft in draft_step.arguments:

                (
                    argument_name,
                    argument,
                ) = self._convert_argument(
                    argument_draft
                )

                if argument_name in arguments:

                    raise PlannerError(
                        "Planner returned duplicate "
                        f"argument: {argument_name}"
                    )

                arguments[
                    argument_name
                ] = argument


            steps.append(
                ExecutionStep(
                    step_number=number,
                    description=description,
                    capability=capability,
                    arguments=arguments,
                    risk=draft_step.risk,
                    permission=(
                        draft_step.permission
                    ),
                )
            )


        step_tuple = tuple(
            steps
        )


        overall_risk = _maximum_risk(
            tuple(
                step.risk
                for step in step_tuple
            )
        )


        return ExecutionPlan(
            request_id=request.request_id,
            steps=step_tuple,
            overall_risk=overall_risk,
        )


    # --------------------------------------------------------
    # PLAN
    # --------------------------------------------------------

    def plan(
        self,
        request: ActionRequest,
    ) -> ExecutionPlan:

        response = (
            self._client
            .responses
            .parse(
                model=self._model,
                instructions=(
                    PLANNER_INSTRUCTIONS
                ),
                input=self._build_input(
                    request
                ),
                text_format=(
                    PlannerDraft
                ),
            )
        )

        draft = (
            response.output_parsed
        )

        if draft is None:

            raise PlannerError(
                "Planner returned no "
                "structured result."
            )

        return self._draft_to_plan(
            request,
            draft,
        )
