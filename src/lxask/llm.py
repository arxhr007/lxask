"""Thin wrapper over llama.cpp for inference on a GGUF model (CPU unless told otherwise)."""

from __future__ import annotations

import os
from pathlib import Path

from . import _core, config

MAX_TOKENS = 160  # a command line or a short list of tasks; anything longer has gone wrong
REPEAT_PENALTY = 1.1
RETRY_REPEAT_PENALTY = 1.3
SAMPLE_TEMPERATURE = 0.7  # for another try at an answer the checks objected to


def default_threads() -> int:
    # llama.cpp scales with physical cores; hyperthreads mostly add contention.
    return max(1, (os.cpu_count() or 2) // 2)


def gpu_layers() -> int:
    """0 on the machines lxask is for. LXASK_GPU_LAYERS=-1 offloads everything, which is
    how the evaluation runs on a GPU without changing what is measured."""
    return int(config.setting("GPU_LAYERS") or "0")


def looks_looped(text: str) -> bool:
    """Whether a reply is stuck repeating itself: the same line three times, or the
    same four words in a row four times (`-S -X -S -X -S -X ...`)."""
    return _core.looks_looped(text)


class Embedder:
    """Sentence embeddings from a small GGUF encoder (bge-small). Call it with a list of texts."""

    MAX_CHARS = 400  # every text we embed is a sentence or two; this keeps batches inside n_ctx

    def __init__(self, model_path: Path, n_threads: int | None = None):
        from llama_cpp import Llama

        self._llm = Llama(
            model_path=str(model_path),
            embedding=True,
            n_ctx=512,
            n_batch=512,
            n_threads=n_threads or default_threads(),
            n_gpu_layers=gpu_layers(),
            verbose=False,
        )

    def __call__(self, texts: list[str]):
        import numpy as np

        vectors = self._llm.embed([text[: self.MAX_CHARS] for text in texts], normalize=True)
        return np.asarray(vectors, dtype=np.float32)


class LLM:
    """The chat model, with the LoRA adapter at `adapter` loaded for it if given, applied
    per request with `specialize` (see lxask.route)."""

    def __init__(self, model_path: Path, n_ctx: int = 2048, n_threads: int | None = None, adapter: Path | None = None):
        from llama_cpp import Llama  # imported here so the rest of lxask works without it

        self._llm = Llama(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=n_threads or default_threads(),
            n_gpu_layers=gpu_layers(),
            # llama.cpp repacks Q4_K weights into its own buffers at load. With mmap the
            # file's pages stay resident as well, so the model is held twice: measured
            # 2.0 GB peak with mmap against 1.4 GB without, for a 1.1 GB model file.
            use_mmap=False,
            verbose=False,
        )
        self._adapter, self._specialized = None, False
        if adapter is not None:
            import llama_cpp

            self._adapter = llama_cpp.llama_adapter_lora_init(self._llm._model.model, str(adapter).encode("utf-8"))
            if not self._adapter:
                raise RuntimeError(f"cannot load the adapter {adapter}")

    def specialize(self, on: bool) -> None:
        """Answer with the adapter applied (`on`), or with the model alone; nothing to do
        without an adapter. The cache holds keys and values computed with the other
        weights, so a switch empties it."""
        if self._adapter is None or on == self._specialized:
            return
        import ctypes

        import llama_cpp

        if on:
            adapters = (llama_cpp.llama_adapter_lora_p_ctypes * 1)(self._adapter)
            failed = llama_cpp.llama_set_adapters_lora(self._llm.ctx, adapters, 1, (ctypes.c_float * 1)(1.0))
        else:
            failed = llama_cpp.llama_set_adapters_lora(self._llm.ctx, None, 0, None)
        if failed:
            raise RuntimeError(f"cannot {'apply' if on else 'remove'} the adapter")
        self._llm.reset()
        self._specialized = on

    def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.0,
        repeat_penalty: float = REPEAT_PENALTY,
        seed: int | None = None,
    ) -> str:
        out = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            **({"seed": seed} if seed is not None else {}),
        )
        # Reasoning models wrap their scratch work in <think>; it is not part of the answer.
        return _core.strip_thinking(out["choices"][0]["message"]["content"] or "")

    def ask(self, messages: list[dict[str, str]], max_tokens: int = MAX_TOKENS) -> str | None:
        """`chat`, guarded against repetition loops: one retry with a stronger penalty,
        then None, so the caller can say plainly that no answer was produced."""
        reply = self.chat(messages, max_tokens=max_tokens)
        if looks_looped(reply):
            reply = self.chat(messages, max_tokens=max_tokens, repeat_penalty=RETRY_REPEAT_PENALTY)
        return None if looks_looped(reply) else reply

    def sample(self, messages: list[dict[str, str]], attempt: int, max_tokens: int = MAX_TOKENS) -> str | None:
        """Another, different answer: sampled instead of greedy, with a seed per attempt
        so that a run can be repeated exactly."""
        reply = self.chat(messages, max_tokens=max_tokens, temperature=SAMPLE_TEMPERATURE, seed=attempt + 1)
        return None if looks_looped(reply) else reply
