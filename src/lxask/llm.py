"""Thin wrapper over llama.cpp for inference on a GGUF model (CPU unless told otherwise)."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING

from . import _core, config

if TYPE_CHECKING:
    from .answer import Search, Step
    from .validate import Catalog

MAX_TOKENS = 160  # a command line or a short list of tasks; anything longer has gone wrong
REPEAT_PENALTY = 1.1
RETRY_REPEAT_PENALTY = 1.3
SAMPLE_TEMPERATURE = 0.7  # for another try at an answer the checks objected to


def default_threads() -> int:
    """One thread per core, as the Rust lxask: every CPU on a machine without
    hyperthreads (unless it is an x86 virtual machine, whose CPUs may be the host's
    hyperthreads), else, or where Linux does not say, half of them."""
    logical = os.cpu_count() or 2
    try:
        lists = [p.read_text() for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology/thread_siblings_list")]
        x86_guest = platform.machine().lower() in ("x86_64", "amd64", "i686", "i386") and any(
            line.startswith("flags") and "hypervisor" in line.split()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
        )
    except OSError:
        lists, x86_guest = [], True
    if lists and not any("," in s or "-" in s for s in lists) and not x86_guest:
        return logical
    return max(1, logical // 2)


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
            n_threads_batch=n_threads or default_threads(),
            n_gpu_layers=gpu_layers(),
            verbose=False,
        )

    def __call__(self, texts: list[str]):
        import numpy as np

        vectors = self._llm.embed([text[: self.MAX_CHARS] for text in texts], normalize=True)
        return np.asarray(vectors, dtype=np.float32)


class LLM:
    """The chat model, with the LoRA adapters it is given loaded for it, applied per
    request with `specialize` (see lxask.route)."""

    def __init__(self, model_path: Path, n_ctx: int = 2048, n_threads: int | None = None,
                 adapters: dict[str, Path] | None = None):
        from llama_cpp import Llama  # imported here so the rest of lxask works without it

        self._llm = Llama(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=n_threads or default_threads(),
            n_threads_batch=n_threads or default_threads(),
            n_gpu_layers=gpu_layers(),
            # llama.cpp repacks Q4_K weights into its own buffers at load. With mmap the
            # file's pages stay resident as well, so the model is held twice: measured
            # 2.0 GB peak with mmap against 1.4 GB without, for a 1.1 GB model file.
            use_mmap=False,
            verbose=False,
        )
        self._adapters: dict[str, object] = {}
        self._specialized: str | None = None
        for name, path in (adapters or {}).items():
            import llama_cpp

            pointer = llama_cpp.llama_adapter_lora_init(self._llm._model.model, str(path).encode("utf-8"))
            if not pointer:
                raise RuntimeError(f"cannot load the adapter {path}")
            self._adapters[name] = pointer

    def specialize(self, which: str | None) -> None:
        """Answer with the adapter named `which` applied, or with the model alone when no
        adapter has that name. The cache holds keys and values computed with the other
        weights, so a switch empties it."""
        if which == self._specialized:
            return
        import ctypes

        import llama_cpp

        if which in self._adapters:
            adapters = (llama_cpp.llama_adapter_lora_p_ctypes * 1)(self._adapters[which])
            failed = llama_cpp.llama_set_adapters_lora(self._llm.ctx, adapters, 1, (ctypes.c_float * 1)(1.0))
        else:
            failed = llama_cpp.llama_set_adapters_lora(self._llm.ctx, None, 0, None)
        if failed:
            raise RuntimeError(f"cannot apply the adapter {which}")
        self._llm.reset()
        self._specialized = which

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

    def answer(self, question: str, search: Search, catalog: Catalog | None, *,
               retry: bool = True, verify: bool = True, adapter: str | None = None) -> list[Step]:
        """Run the shared answer flow with the same adapter choices as the program.

        Every generation restores the routed adapter: a verification call may have
        switched to precision before a retry or the next step.
        """
        from .answer import answer
        from .route import adapter as route_adapter

        routed = adapter or route_adapter(question, catalog)

        def chat(messages):
            self.specialize(routed)
            return self.ask(messages)

        def resample(messages, attempt):
            self.specialize(routed)
            return self.sample(messages, attempt)

        def look(messages):
            self.specialize("precision")
            return self.ask(messages)

        return answer(question, chat, search, catalog,
                      resample=resample if retry else None, verify=look if verify else None)
