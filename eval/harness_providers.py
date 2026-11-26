"""Deterministic stand-in providers used to verify the evaluation HARNESS without an LLM.

They are not language models and nothing they score says anything about model quality:

  oracle   returns the golden reference SQL (or the adversarial `attack_sql`, or a
           clarification for unanswerable questions). Expected outcome by construction:
           100% execution accuracy and every attack rejected. If a number here is lower, the
           harness, a gate or the dataset is broken -- that is what the run is for.
  faulty   oracle, except that on the FIRST attempt for every 3rd answerable question it
           emits SQL referencing a nonexistent column (a fixable error). It recovers only if
           the pipeline feeds the validator error back and retries, so it isolates and
           measures the retry mechanism, independent of any model.
"""

from __future__ import annotations

from nl2sql.llm import LLMResponse
from nl2sql.prompt import BuiltPrompt

RETRY_MARKER = "Your previous attempt was rejected before it ran."


class OracleProvider:
    name = "oracle"
    FAULT_EVERY = 0  # 0 = never inject a fault

    def __init__(self, items: list[dict]) -> None:
        self.by_question = {it["question"]: it for it in items}
        answerable = [it["id"] for it in items if it["expect"] == "answer"]
        self.faulty_ids = (
            {qid for i, qid in enumerate(answerable) if i % self.FAULT_EVERY == 0}
            if self.FAULT_EVERY
            else set()
        )

    def generate(self, prompt: BuiltPrompt) -> LLMResponse:
        question = prompt.user.split("\n", 1)[0].removeprefix("Question: ")
        item = self.by_question[question]
        is_retry = RETRY_MARKER in prompt.user
        if item["expect"] == "unanswerable":
            return LLMResponse(
                "clarify", None, "That information is not in this database.",
                "Not answerable from the schema.", provider=self.name, model=self.name,
            )
        sql = item["attack_sql"] if item["expect"] == "reject" else item["reference_sql"]
        if item["id"] in self.faulty_ids and not is_retry:
            sql = sql.replace("SELECT ", "SELECT zz_bogus_col AS zz, ", 1)
        return LLMResponse(
            "sql", sql, None, f"oracle answer for {item['id']}", provider=self.name, model=self.name
        )


class FaultyOracleProvider(OracleProvider):
    name = "faulty"
    FAULT_EVERY = 3


PROVIDERS = {"oracle": OracleProvider, "faulty": FaultyOracleProvider}
