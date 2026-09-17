from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import numpy as np

from bioes import Span, merge_window_spans, spans_from_path, viterbi_decode


DEFAULT_MODEL = "OpenMed/privacy-filter-nemotron-v2"


class Backend(Protocol):
    model_name: str
    model_revision: str
    backend_name: str

    def predict(self, texts: Sequence[str]) -> list[list[Span]]: ...


@dataclass
class Window:
    owner: int
    input_ids: list[int]
    offsets: list[tuple[int, int]]


def _make_windows(
    tokenizer: Any,
    texts: Sequence[str],
    max_tokens: int,
    overlap_tokens: int,
) -> list[Window]:
    windows: list[Window] = []
    for owner, text in enumerate(texts):
        encoded = tokenizer(
            text,
            add_special_tokens=False,
            truncation=True,
            max_length=max_tokens,
            stride=overlap_tokens,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
        )
        input_rows = encoded["input_ids"]
        offset_rows = encoded["offset_mapping"]
        if input_rows and isinstance(input_rows[0], int):
            input_rows = [input_rows]
            offset_rows = [offset_rows]
        for input_ids, offsets in zip(input_rows, offset_rows):
            if not input_ids:
                continue
            windows.append(
                Window(
                    owner=owner,
                    input_ids=[int(value) for value in input_ids],
                    offsets=[(int(start), int(end)) for start, end in offsets],
                )
            )
    return windows


def _decode_window(
    text: str,
    offsets: list[tuple[int, int]],
    scores: np.ndarray,
    labels: list[str],
    *,
    activated: bool,
) -> list[Span]:
    values = np.asarray(scores, dtype=np.float32)
    if activated:
        probabilities = np.clip(values, 1e-12, 1.0)
        log_scores = np.log(probabilities)
    else:
        shifted = values - values.max(axis=-1, keepdims=True)
        exp_values = np.exp(shifted)
        probabilities = exp_values / exp_values.sum(axis=-1, keepdims=True)
        log_scores = shifted - np.log(exp_values.sum(axis=-1, keepdims=True))
    path = viterbi_decode(log_scores, labels)
    return spans_from_path(text, offsets, probabilities, labels, path)


class TransformersBackend:
    backend_name = "transformers"

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        device: str = "cuda",
        batch_size: int = 16,
        max_tokens: int = 4096,
        overlap_tokens: int = 256,
    ) -> None:
        import torch
        from transformers import AutoConfig, AutoModelForTokenClassification, AutoTokenizer

        self.torch = torch
        self.model_name = model_name
        self.device = torch.device(device)
        self.batch_size = batch_size
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
        self.model_revision = str(getattr(config, "_commit_hash", None) or "unknown")
        self.labels = [
            str(config.id2label[index]) for index in range(int(config.num_labels))
        ]
        self.model = AutoModelForTokenClassification.from_pretrained(
            model_name,
            trust_remote_code=True,
            dtype=torch.bfloat16 if self.device.type == "cuda" else torch.float32,
        )
        self.model.to(self.device)
        self.model.eval()
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

    def predict(self, texts: Sequence[str]) -> list[list[Span]]:
        torch = self.torch
        windows = _make_windows(
            self.tokenizer,
            texts,
            self.max_tokens,
            self.overlap_tokens,
        )
        by_owner: list[list[Span]] = [[] for _ in texts]
        windows.sort(key=lambda item: len(item.input_ids))
        with torch.inference_mode():
            for start in range(0, len(windows), self.batch_size):
                batch = windows[start : start + self.batch_size]
                max_length = max(len(window.input_ids) for window in batch)
                input_ids = []
                attention_masks = []
                for window in batch:
                    pad = max_length - len(window.input_ids)
                    input_ids.append(
                        window.input_ids + [self.tokenizer.pad_token_id] * pad
                    )
                    attention_masks.append([1] * len(window.input_ids) + [0] * pad)
                model_output = self.model(
                    input_ids=torch.tensor(
                        input_ids,
                        dtype=torch.long,
                        device=self.device,
                    ),
                    attention_mask=torch.tensor(
                        attention_masks,
                        dtype=torch.long,
                        device=self.device,
                    ),
                )
                logits = model_output.logits.float().cpu().numpy()
                for window, scores in zip(batch, logits):
                    length = len(window.input_ids)
                    by_owner[window.owner].extend(
                        _decode_window(
                            texts[window.owner],
                            window.offsets,
                            scores[:length],
                            self.labels,
                            activated=False,
                        )
                    )
        return [merge_window_spans(spans) for spans in by_owner]


class VllmBackend:
    backend_name = "vllm"

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        batch_size: int = 32,
        max_tokens: int = 4096,
        overlap_tokens: int = 256,
        gpu_memory_utilization: float = 0.9,
    ) -> None:
        from transformers import AutoConfig, AutoTokenizer
        from vllm import LLM
        from vllm.config import PoolerConfig
        from vllm.model_executor.models import ModelRegistry

        architecture = "OpenAIPrivacyFilterForTokenClassification"
        if architecture not in ModelRegistry.get_supported_archs():
            raise RuntimeError(
                f"Installed vLLM does not support {architecture}; "
                "use --backend transformers"
            )
        self.model_name = model_name
        self.batch_size = batch_size
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
        self.model_revision = str(getattr(config, "_commit_hash", None) or "unknown")
        self.labels = [
            str(config.id2label[index]) for index in range(int(config.num_labels))
        ]
        self.llm = LLM(
            model=model_name,
            runner="pooling",
            pooler_config=PoolerConfig(task="token_classify"),
            trust_remote_code=True,
            max_model_len=max_tokens,
            dtype="bfloat16",
            gpu_memory_utilization=gpu_memory_utilization,
        )

    def predict(self, texts: Sequence[str]) -> list[list[Span]]:
        windows = _make_windows(
            self.tokenizer,
            texts,
            self.max_tokens,
            self.overlap_tokens,
        )
        by_owner: list[list[Span]] = [[] for _ in texts]
        windows.sort(key=lambda item: len(item.input_ids))
        for start in range(0, len(windows), self.batch_size):
            batch = windows[start : start + self.batch_size]
            prompts = [{"prompt_token_ids": window.input_ids} for window in batch]
            outputs = self.llm.encode(prompts, pooling_task="token_classify")
            if len(outputs) != len(batch):
                raise RuntimeError("vLLM returned an unexpected number of outputs")
            for window, output in zip(batch, outputs):
                data = output.outputs.data
                scores = data.float().cpu().numpy() if hasattr(data, "cpu") else np.asarray(data)
                by_owner[window.owner].extend(
                    _decode_window(
                        texts[window.owner],
                        window.offsets,
                        scores[: len(window.input_ids)],
                        self.labels,
                        activated=True,
                    )
                )
        return [merge_window_spans(spans) for spans in by_owner]


class OpenMedReferenceBackend:
    backend_name = "openmed"

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        from openmed import extract_pii
        from transformers import AutoConfig

        self.model_name = model_name
        config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
        self.model_revision = str(getattr(config, "_commit_hash", None) or "unknown")
        self.extract_pii = extract_pii

    def predict(self, texts: Sequence[str]) -> list[list[Span]]:
        results: list[list[Span]] = []
        for text in texts:
            result = self.extract_pii(
                text,
                model_name=self.model_name,
                confidence_threshold=0.0,
                use_smart_merging=True,
            )
            entities = getattr(result, "entities", ())
            spans = []
            for entity in entities:
                start = int(getattr(entity, "start"))
                end = int(getattr(entity, "end"))
                spans.append(
                    Span(
                        label=str(getattr(entity, "label")),
                        start=start,
                        end=end,
                        text=text[start:end],
                        confidence=float(getattr(entity, "confidence", 1.0)),
                    )
                )
            results.append(spans)
        return results


def create_backend(
    backend: str,
    model_name: str,
    *,
    batch_size: int,
    max_tokens: int,
    overlap_tokens: int,
) -> Backend:
    if backend == "transformers":
        return TransformersBackend(
            model_name,
            batch_size=batch_size,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )
    if backend == "vllm":
        return VllmBackend(
            model_name,
            batch_size=batch_size,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )
    if backend == "openmed":
        return OpenMedReferenceBackend(model_name)
    raise ValueError(f"Unsupported backend: {backend}")

