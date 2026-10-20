"""The official prompts and scoring code, loaded from the pinned upstream files.

Only the pieces needed are taken from each file: string constants are read with
`ast.literal_eval`, and the scoring functions are compiled from their own definitions, so nothing
else in those modules (imports, model set-up, network clients) runs here. The judge model is passed
in through the same argument the upstream code uses.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Callable, List, Tuple

from .sources import fetch


class OfficialCodeError(RuntimeError):
    pass


def _tree(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def _constant(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise OfficialCodeError(f"{name} not found as a module-level constant")


def _functions(tree: ast.Module, names: list[str]) -> list[ast.FunctionDef]:
    found = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names}
    missing = [n for n in names if n not in found]
    if missing:
        raise OfficialCodeError(f"functions not found: {missing}")
    return [found[n] for n in names]


def _compile(nodes: list[ast.FunctionDef], filename: str, namespace: dict[str, Any]) -> dict[str, Any]:
    module = ast.Module(body=list(nodes), type_ignores=[])
    exec(compile(module, filename, "exec"), namespace)  # noqa: S102 - pinned, hash-checked upstream code
    return namespace


class LongMemEvalOfficial:
    """`get_anscheck_prompt` and the judge model table from evaluate_qa.py; reader templates from run_generation.py."""

    def __init__(self) -> None:
        _, tree = _tree(fetch("longmemeval/evaluate_qa.py"))
        ns = _compile(_functions(tree, ["get_anscheck_prompt"]), "longmemeval/evaluate_qa.py", {})
        self.anscheck_prompt: Callable[..., str] = ns["get_anscheck_prompt"]
        self.model_zoo: dict[str, tuple[str, str]] = _constant(tree, "model_zoo")

        _, gen = _tree(fetch("longmemeval/run_generation.py"))
        prepare = _functions(gen, ["prepare_prompt"])[0]
        templates = []
        for node in ast.walk(prepare):
            if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "answer_prompt_template"
                                                     for t in node.targets)
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                templates.append(node.value.value)
        # The variant without fact expansion: "...based on the relevant chat history" (CoT and plain).
        plain_history = [t for t in templates if "History Chats" in t and "facts" not in t]
        cot = [t for t in plain_history if "step by step" in t]
        plain = [t for t in plain_history if "step by step" not in t]
        if len(cot) != 1 or len(plain) != 1:
            raise OfficialCodeError("could not identify the LongMemEval reader templates")
        self.reader_template_cot = cot[0]
        self.reader_template_plain = plain[0]

    def judge_model(self, short_or_full: str) -> str:
        """Map the official short names (gpt-4o) to the pinned snapshot; other names pass through."""
        if short_or_full in self.model_zoo:
            return self.model_zoo[short_or_full][0]
        return short_or_full


class LoCoMoJudge:
    """The LLM-judge ("J") prompt that most published LoCoMo comparisons use, from mem0's evaluation code."""

    def __init__(self) -> None:
        _, tree = _tree(fetch("mem0/llm_judge.py"))
        self.prompt: str = _constant(tree, "ACCURACY_PROMPT")
        for field in ("{question}", "{gold_answer}", "{generated_answer}"):
            if field not in self.prompt:
                raise OfficialCodeError(f"ACCURACY_PROMPT has no {field} placeholder")

    def render(self, question: str, gold_answer: str, generated_answer: str) -> str:
        return self.prompt.format(question=question, gold_answer=gold_answer, generated_answer=generated_answer)

    @staticmethod
    def label(response: str) -> str | None:
        text = response.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            value = json.loads(text).get("label")
            return value.strip() if isinstance(value, str) else None
        except (json.JSONDecodeError, AttributeError):
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    value = json.loads(match.group(0)).get("label")
                    return value.strip() if isinstance(value, str) else None
                except (json.JSONDecodeError, AttributeError):
                    return None
        return None


BEAM_ABILITIES = (
    "abstention", "contradiction_resolution", "event_ordering", "information_extraction",
    "instruction_following", "knowledge_update", "multi_session_reasoning", "preference_following",
    "summarization", "temporal_reasoning",
)


class BeamOfficial:
    """BEAM's per-ability evaluators from compute_metrics.py and its prompts from prompts.py.

    Each evaluator is called as BEAM's run_evaluation.py calls it, with the judge passed as `model`.
    The per-question score follows report_results.py: tau_norm for event ordering, llm_judge_score
    for every other ability.
    """

    def __init__(self) -> None:
        from json_repair import repair_json
        from scipy.stats import kendalltau

        _, prompts = _tree(fetch("beam/prompts.py"))
        self.judge_prompt: str = _constant(prompts, "unified_llm_judge_base_prompt")
        self.answer_prompt: str = _constant(prompts, "answer_generation_for_rag")
        for marker in ("<context>", "<question>"):
            if marker not in self.answer_prompt:
                raise OfficialCodeError(f"answer_generation_for_rag has no {marker} marker")

        _, metrics = _tree(fetch("beam/compute_metrics.py"))
        names = ["parse_json_response", "llm_equivalence", "align_with_llm", "event_ordering_score"]
        names += [f"evaluate_{ability}" for ability in BEAM_ABILITIES]

        def semantic_align(*_args, **_kwargs):  # the embedding-based variant is not used by the evaluators
            raise OfficialCodeError("semantic alignment is not available in this harness")

        namespace = {"json": json, "re": re, "List": List, "Tuple": Tuple, "kendalltau": kendalltau,
                     "repair_json": repair_json, "unified_llm_judge_base_prompt": self.judge_prompt,
                     "semantic_align": semantic_align}
        ns = _compile(_functions(metrics, names), "beam/compute_metrics.py", namespace)
        self.evaluators: dict[str, Callable[..., dict]] = {a: ns[f"evaluate_{a}"] for a in BEAM_ABILITIES}

    def answer(self, context: str, question: str) -> str:
        return self.answer_prompt.replace("<context>", context).replace("<question>", question)

    def evaluate(self, ability: str, rubric: list, response: str, question: str, judge: Any) -> dict:
        return self.evaluators[ability](rubric=rubric, llm_response=response, probing_question=question, model=judge)

    @staticmethod
    def score(ability: str, result: dict) -> float:
        return float(result["tau_norm"] if ability == "event_ordering" else result["llm_judge_score"])
