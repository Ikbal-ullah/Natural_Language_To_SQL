"""Model providers.

Two are implemented and both are real:

`AnthropicProvider` is the production path. It calls Claude with the schema-aware prompt,
constrains the answer to a JSON schema, and marks the stable prefix for caching.

`RetrievalProvider` is a genuine baseline, not a placeholder: it answers by returning the
SQL of the nearest example in the few-shot bank. It exists for two reasons. First, it lets
the entire pipeline -- validation, plan gate, execution, evaluation -- run and be tested
with no API key and no network. Second, and more usefully, it is the control the model has
to beat. If an LLM scores 40% and nearest-neighbour retrieval scores 35%, the model is
barely contributing and the honest thing is to know that. BENCHMARKS.md reports both.

PROMPT CACHING
--------------
The system message (role + schema + rules + examples) is thousands of tokens and is
identical across requests with the same configuration. The question is a few dozen tokens
and changes every time. Caching is a prefix match, so the split matters: stable content in
`system` with a cache breakpoint, volatile content in `messages`. Get this backwards --
put the question in the system prompt -- and the cache never hits, which shows up as a
bill roughly 10x larger than it needed to be and nothing else. `usage.cache_read_input_tokens`
in the response is how you check, and the pipeline records it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from nl2sql.config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from anthropic.types import MessageParam, TextBlockParam
    from anthropic.types.output_config_param import OutputConfigParam
from nl2sql.examples import select_examples
from nl2sql.prompt import RESPONSE_SCHEMA, BuiltPrompt


class LLMError(RuntimeError):
    """Raised when the provider cannot produce a usable answer."""


@dataclass
class LLMResponse:
    """What a provider returns, normalised across providers."""

    action: str
    sql: str | None
    clarification_question: str | None
    interpretation: str
    assumptions: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider: str = ""
    model: str = ""

    @property
    def wants_clarification(self) -> bool:
        return self.action == "clarify"


class Provider(Protocol):
    """The whole surface the pipeline depends on."""

    name: str

    def generate(self, prompt: BuiltPrompt) -> LLMResponse: ...


def _coerce(payload: dict[str, Any]) -> tuple[str, str | None, str | None, str, list[str]]:
    """Normalise a model payload, tolerating the shapes a model actually produces.

    Structured outputs make this mostly unnecessary, but 'mostly' is doing real work: a
    model can satisfy the schema and still return action='sql' with sql=null. Deciding
    here what that means -- rather than letting a None reach the validator and surface as
    a confusing EMPTY error three layers down -- keeps the failure legible.
    """
    action = str(payload.get("action") or "sql").strip().lower()
    sql = payload.get("sql")
    clarification = payload.get("clarification_question")
    interpretation = str(payload.get("interpretation") or "").strip()
    assumptions = [str(a) for a in (payload.get("assumptions") or [])]
    sql = sql.strip() if isinstance(sql, str) and sql.strip() else None
    clarification = (
        clarification.strip()
        if isinstance(clarification, str) and clarification.strip()
        else None
    )
    if action not in ("sql", "clarify"):
        action = "clarify" if clarification and not sql else "sql"
    if action == "sql" and sql is None:
        action = "clarify"
        clarification = clarification or "I could not construct a query for that question."
    return action, sql, clarification, interpretation, assumptions


class AnthropicProvider:
    """The production path: Claude with a schema-constrained answer."""

    name = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.anthropic_api_key:
            # Not fatal at import time -- the SDK also reads ANTHROPIC_API_KEY and an
            # `ant auth login` profile from the environment. We only fail when a call is
            # actually attempted with no credential anywhere.
            pass
        try:
            import anthropic
        except ImportError as err:  # pragma: no cover
            raise LLMError("the `anthropic` package is not installed") from err
        kwargs: dict[str, Any] = {"max_retries": 3, "timeout": 60.0}
        if self.settings.anthropic_api_key:
            kwargs["api_key"] = self.settings.anthropic_api_key
        self._client = anthropic.Anthropic(**kwargs)
        self._anthropic = anthropic

    def generate(self, prompt: BuiltPrompt) -> LLMResponse:
        # The cache breakpoint goes at the end of the stable prefix. Everything before it
        # -- role, schema, rules, examples -- is reused across requests at roughly a tenth
        # of the input price.
        system: list[TextBlockParam] = [
            {
                "type": "text",
                "text": prompt.system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        messages: list[MessageParam] = [{"role": "user", "content": prompt.user}]
        output_config: OutputConfigParam = {
            "effort": self.settings.effort,  # type: ignore[typeddict-item]
            "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
        }
        try:
            response = self._client.messages.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=system,
                messages=messages,
                output_config=output_config,
            )
        except self._anthropic.APIStatusError as err:
            raise LLMError(f"Claude API error {err.status_code}: {err.message}") from err
        except self._anthropic.APIConnectionError as err:
            raise LLMError(f"could not reach the Claude API: {err}") from err

        # A refusal is a real outcome with a real stop_reason; treating it as a parse
        # failure would send the retry loop chasing a problem that does not exist.
        if getattr(response, "stop_reason", None) == "refusal":
            detail = getattr(response, "stop_details", None)
            reason = getattr(detail, "explanation", None) or "the request was declined"
            raise LLMError(f"model declined to answer: {reason}")

        # `block.type` is a literal discriminator, so comparing it narrows the union to
        # TextBlock; getattr() would work at runtime but leaves the type unnarrowed.
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            raise LLMError("model returned no text content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise LLMError(f"model output was not valid JSON: {text[:200]}") from err

        action, sql, clarification, interpretation, assumptions = _coerce(payload)
        usage = response.usage
        return LLMResponse(
            action=action,
            sql=sql,
            clarification_question=clarification,
            interpretation=interpretation,
            assumptions=assumptions,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            provider=self.name,
            model=self.settings.model,
        )

    def count_tokens(self, prompt: BuiltPrompt) -> int:
        """Exact prompt size from the API, for the cost table in BENCHMARKS.md.

        Counting tokens by dividing characters by four is an estimate that is wrong by
        10-20% on SQL, which is dense in punctuation. This is the real number.
        """
        system: list[TextBlockParam] = [{"type": "text", "text": prompt.system}]
        messages: list[MessageParam] = [{"role": "user", "content": prompt.user}]
        result = self._client.messages.count_tokens(
            model=self.settings.model, system=system, messages=messages
        )
        return int(result.input_tokens)


class RetrievalProvider:
    """Nearest-neighbour baseline: answer with the most similar known query.

    Deterministic, free, offline, and a fair control. It cannot generalise -- it returns
    an example verbatim, so it is right only when the question is close to one it has
    seen -- and it can never ask for clarification. Both limitations are honest and both
    show up in the per-category evaluation breakdown.
    """

    name = "retrieval"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate(self, prompt: BuiltPrompt) -> LLMResponse:
        question = prompt.user.split("\n", 1)[0].removeprefix("Question: ")
        matches = select_examples(question, 1)
        if not matches:
            raise LLMError("no example available")
        best = matches[0]
        return LLMResponse(
            action="sql",
            sql=best.sql,
            clarification_question=None,
            interpretation=(
                f"Reused the query for a similar known question: {best.question!r}. "
                "This is the retrieval baseline, which cannot adapt SQL to a new question."
            ),
            assumptions=["Answer copied verbatim from the nearest example."],
            provider=self.name,
            model="nearest-example",
        )


def get_provider(settings: Settings | None = None) -> Provider:
    """Build the configured provider.

    Falls back to the retrieval baseline when `anthropic` is selected but no credential is
    present, so that `make eval` works on a fresh clone. The fallback is loud: the
    provider name travels with every result and appears in the report, so a benchmark can
    never silently claim to be measuring the model when it is measuring the baseline.
    """
    settings = settings or get_settings()
    choice = settings.llm_provider.lower()
    if choice == "retrieval":
        return RetrievalProvider(settings)
    if choice != "anthropic":
        raise LLMError(f"unknown provider {settings.llm_provider!r}")

    import os

    has_credential = bool(
        settings.anthropic_api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if not has_credential:
        return RetrievalProvider(settings)
    return AnthropicProvider(settings)
