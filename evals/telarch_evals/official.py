"""The official prompts and scoring code, loaded from the pinned upstream files.

Only the pieces needed are taken from each file: string constants are read with
`ast.literal_eval`, and the scoring functions are compiled from their own definitions, so nothing
else in those modules (imports, model set-up, network clients) runs here. The judge model is passed
in through the same argument the upstream code uses.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import string
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .sources import SOURCES, fetch, home


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


def _assignments(tree: ast.Module, names: Sequence[str] | None = None) -> list[ast.stmt]:
    """Module-level assignments, all of them or only those that bind one of `names`."""
    return [node for node in tree.body if isinstance(node, ast.Assign)
            and (names is None or any(isinstance(t, ast.Name) and t.id in names for t in node.targets))]


def _compile(nodes: Sequence[ast.stmt], filename: str, namespace: dict[str, Any]) -> dict[str, Any]:
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

        namespace = {"json": json, "re": re, "List": list, "Tuple": tuple, "kendalltau": kendalltau,
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


# The metric code of MemoryAgentBench's utils/eval_other_utils.py, in dependency order.
MAB_METRICS = [
    "normalize_answer", "f1_score", "drqa_exact_match_score", "substring_exact_match_score",
    "drqa_metric_max_over_ground_truths", "parse_output", "chunk_text_into_sentences", "clean_text_elements",
    "clean_parentheses", "normalize_whitespace", "remove_numbering", "extract_movie_name", "find_nearest_movie",
    "extract_recommendation_list", "calculate_metrics", "post_process", "_process_icl_dataset",
    "_process_eventqa_dataset", "_process_infbench_longmemeval_dataset", "_process_choice_eng_dataset",
    "_process_ruler_memory_merging_dataset", "_process_recsys_dataset", "default_post_process",
]


def _nltk_data() -> Path:
    """Unpack the pinned punkt_tab sentence tokenizer once and return the NLTK data directory."""
    root = home() / "nltk_data"
    marker = root / "tokenizers" / "punkt_tab" / ".sha256"
    digest = SOURCES["nltk/punkt_tab.zip"].sha256 or ""
    if not (marker.exists() and marker.read_text() == digest):
        (root / "tokenizers").mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(fetch("nltk/punkt_tab.zip")) as archive:
            archive.extractall(root / "tokenizers")
        marker.write_text(digest)
    return root


class MemoryAgentBenchOfficial:
    """MemoryAgentBench's templates, chunker, metrics and judge prompts, from the pinned repository files.

    The functions run as written. Two things around them are supplied here: nltk.download is a no-op
    (the pinned punkt_tab tokenizer is unpacked under the cache), and the movie list that
    _process_recsys_dataset opens at ./processed_data/Recsys_Redial/entity2id.json comes from the
    pinned copy of the dataset's entity2id.json.
    """

    def __init__(self) -> None:
        import nltk
        import numpy as np
        import tiktoken
        from editdistance import eval as edit_distance
        from rouge_score import rouge_scorer

        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(home() / "tiktoken"))
        data_dir = str(_nltk_data())
        if data_dir not in nltk.data.path:
            nltk.data.path.insert(0, data_dir)

        _, templates = _tree(fetch("memoryagentbench/templates.py"))
        ns = _compile([n for n in templates.body if isinstance(n, (ast.Assign, ast.FunctionDef))],
                      "memoryagentbench/templates.py", {})
        self.get_template: Callable[[str, str, str], str] = ns["get_template"]
        self.system_message: str = ns["SYSTEM_MESSAGE"]

        movies = str(fetch("memoryagentbench/entity2id.json"))

        def pinned_open(path, *args, **kwargs):
            if str(path).replace("\\", "/").endswith("Recsys_Redial/entity2id.json"):
                path = movies
            return open(path, *args, **kwargs)

        _, utils = _tree(fetch("memoryagentbench/eval_other_utils.py"))
        namespace = {
            "re": re, "string": string, "json": json, "os": os, "np": np, "Counter": Counter,
            "tiktoken": tiktoken, "rouge_scorer": rouge_scorer, "edit_distance": edit_distance,
            "nltk": SimpleNamespace(download=lambda *_a, **_k: True, sent_tokenize=nltk.sent_tokenize),
            "logger": logging.getLogger("telarch_evals.memoryagentbench"), "open": pinned_open,
        }
        ns = _compile(_assignments(utils, ["rouge_scorer_instance"]) + _functions(utils, MAB_METRICS),
                      "memoryagentbench/eval_other_utils.py", namespace)
        self.chunk: Callable[..., list[str]] = ns["chunk_text_into_sentences"]
        self.post_process: Callable[..., tuple[dict, dict]] = ns["post_process"]
        self.sent_tokenize: Callable[[str], list[str]] = nltk.sent_tokenize
        self.encoding = tiktoken.encoding_for_model("gpt-4o-mini")

        _, lme = _tree(fetch("memoryagentbench/longmem_qa_evaluate.py"))
        self.anscheck_prompt: Callable[..., str] = _compile(
            _functions(lme, ["get_anscheck_prompt"]), "memoryagentbench/longmem_qa_evaluate.py", {})["get_anscheck_prompt"]

        _, summary = _tree(fetch("memoryagentbench/summarization_evaluate.py"))
        self.summary_templates = [_constant(summary, name) for name in
                                  ("fluency_prompt_book", "recall_prompt_book", "precision_prompt_book")]
        self.parse_json: Callable[[str], Any] = _compile(
            _functions(summary, ["parse_json"]), "memoryagentbench/summarization_evaluate.py",
            {"re": re, "json": json})["parse_json"]

    def tokens(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))

    def summary_prompts(self, output: str, keypoints: list, answer: Any) -> list[str]:
        """The fluency, recall and precision prompts for one ∞Bench-Sum summary, built as check_metrics does."""
        text = output.strip()
        fluency, recall, precision = self.summary_templates
        points = "\n".join(f"{i + 1}. {kp}" for i, kp in enumerate(keypoints))
        return [fluency.format(text=text), recall.format(keypoints=points, summary=text),
                precision.format(expert_summary=answer[0], summary=text)]

    @staticmethod
    def summary_score(fluency: dict, recall: dict, precision: dict, keypoints: list) -> dict:
        """check_metrics' score: key-point recall, sentence precision, and their F1 times fluency."""
        rec = recall["recall"] / len(keypoints) if len(keypoints) > 0 else 0
        prec = precision["precision"] / precision["sentence_count"] if precision["sentence_count"] > 0 else 0
        f1 = fluency["fluency"] * 2 * (rec * prec) / (rec + prec) if rec + prec > 0 else 0
        return {"f1": f1, "fluency": fluency["fluency"], "recall": rec, "precision": prec}
