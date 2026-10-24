# Architecture

lxask is one Rust program and four files: the model, its restriction adapter, a small
encoder and a SQLite index of documentation. Everything runs on your machine, on the
CPU. Python is used only to build those files and to measure the result, and it calls
the same Rust core the program uses, so what is measured is what ships.

For the reasoning behind each step in prose, see [how-it-works.md](how-it-works.md);
for numbers, [results.md](results.md).

## Answering a request

What happens between `lxask "..."` and the printed command
(`rust/crates/lxask/src/main.rs`, `rust/crates/lxask-core/src/answer.rs`):

```mermaid
flowchart TD
    q(["lxask 'what you want to do'"]) --> about{"about lxask itself?<br/>about::about_itself"}
    about -- yes --> abouttext["print who made lxask<br/>exit 0, no model"]
    about -- no --> vague{"too vague?<br/>clarify::clarification"}
    vague -- yes --> askback["ask back which one is meant<br/>exit 3, no model"]
    vague -- no --> server{"lxask --serve running?"}
    server -- yes --> viaserver["the server answers<br/>with the same steps below"]
    server -- no --> load["load model + adapter, encoder,<br/>index and catalogue<br/>Loaded::load"]
    load --> route{"rules out a program<br/>or about a proxy?<br/>route::specialist"}
    route -- yes --> adapteron["apply the restriction adapter<br/>Llm::specialize"]
    route -- no --> adapteroff["model alone"]
    adapteron --> split
    adapteroff --> split
    split{"several parts?<br/>split::needs_split"} -- yes --> tasks["one model call:<br/>request to tasks<br/>retried if a file pattern,<br/>size or age is lost"]
    split -- no --> one["one task"]
    tasks --> each
    one --> each
    each["for each task"] --> retrieve["retrieve notes: embed the task,<br/>pick 4 likely commands,<br/>take their closest examples,<br/>drop notes on ruled-out programs"]
    retrieve --> prompt["build the prompt<br/>task + notes, at most 1,800 chars"]
    prompt --> model["model writes one command<br/>greedy"]
    model --> repair["repair slips with one right fix<br/>constraints::repair"]
    repair --> check{"objections?<br/>validate::check: programs, options,<br/>subcommands, shell syntax<br/>constraints::check: literals,<br/>known slips, ruled-out programs"}
    check -- "yes, tries left" --> retry["sample another answer<br/>up to 2 more tries"]
    retry --> repair
    check -- "none, or no tries left" --> keep["keep the answer with<br/>the fewest objections"]
    keep --> explain["explain each program and option<br/>from tldr and man pages"]
    explain --> warn["warn about destructive commands<br/>safety::find_warnings"]
    warn --> render["print: numbered steps,<br/>unverified: and note: lines"]
    render --> last["remember in last.json<br/>for lxask --bad"]
```

Two shortcuts answer without the model at all: questions about lxask itself, and
requests too vague for one command ("add a proxy"). Everything else goes through the
model, one call per task plus one for the split, and up to two more per task when the
checks object.

## Components

```mermaid
flowchart LR
    subgraph program ["lxask program  rust/crates/lxask"]
        main["main.rs<br/>options, about, ask-back,<br/>batch for evaluation"]
        serverrs["server.rs<br/>Unix socket server"]
        feedback["feedback.rs<br/>last.json, --bad, --share"]
        config["config.rs<br/>LXASK_* settings, file paths"]
    end
    subgraph core ["lxask-core  the logic"]
        answer["answer<br/>the flow, retries"]
        splitrs["split"]
        retrievers["retrieve"]
        promptrs["prompt"]
        validate["validate<br/>catalogue, options,<br/>subcommands"]
        constraints["constraints<br/>literals, slips, repairs,<br/>ruled-out programs"]
        routers["route"]
        clarify["clarify"]
        aboutrs["about"]
        explainrs["explain"]
        safety["safety"]
        shlex["shlex"]
    end
    subgraph llm ["lxask-llm"]
        chat["chat model<br/>+ LoRA adapter"]
        embedder["encoder"]
    end
    subgraph files ["~/.local/share/lxask"]
        modelfile[("model.gguf<br/>Qwen2.5-Coder-1.5B fine-tune")]
        adapterfile[("restrict.gguf<br/>LoRA adapter")]
        embedfile[("embed.gguf<br/>bge-small")]
        index[("docs.sqlite<br/>tldr + man index,<br/>embeddings, catalogue")]
    end
    py["lxask-py<br/>lxask._core for Python"] --> core
    scripts["scripts/ and src/lxask<br/>training, evaluation"] --> py
    main --> llm
    serverrs --> main
    feedback --> main
    config --> main
    main --> aboutrs & clarify & routers & answer
    answer --> splitrs & retrievers & promptrs & validate & constraints & explainrs & safety
    validate --> shlex
    chat --> llamacpp["llama.cpp<br/>via llama-cpp-2"]
    embedder --> llamacpp
    chat --- modelfile
    chat --- adapterfile
    embedder --- embedfile
    retrievers --- index
    validate --- index
```

| crate | what it is |
|---|---|
| `lxask` | the program: options, the server, feedback, settings |
| `lxask-core` | every rule, with no I/O: the model and the search are passed in, so it is tested without them |
| `lxask-llm` | the model, the adapter and the encoder, through llama.cpp |
| `lxask-py` | the core as the Python module `lxask._core`, used by `src/lxask` and `scripts/` |

## The server

`lxask --serve` keeps everything loaded, so later questions skip the few seconds of
loading. With `serve = auto` (the installer's default) a question that finds no
server starts one in the background, detached from the terminal, and waits for it;
a lock next to the socket makes sure only one is started (`server::start_and_wait`).

```mermaid
sequenceDiagram
    participant U as lxask 'question'
    participant S as lxask --serve
    Note over S: loads model, adapter,<br/>encoder and index once
    S->>S: listen on $XDG_RUNTIME_DIR/lxask/<br/>(only this user may connect)
    U->>U: about lxask itself or too vague?<br/>answered here, no server needed
    U->>S: {"type": "ask", "question": ...}
    alt same version
        S-->>U: {"stdout": ..., "stderr": ..., "code": 0}
    else server is an older version
        S-->>U: {"stale": "0.9.0"}
        U->>U: answers on its own
    end
    Note over S: stops after 30 minutes without a question,<br/>or on lxask --stop
```

## Feedback

```mermaid
flowchart LR
    ans["every answer"] --> lastjson[("last.json")]
    bad["lxask --bad 'what was wrong'"] --> fb[("feedback.jsonl")]
    lastjson --> bad
    fb --> share["lxask --share<br/>prints the reports and a link<br/>to a prefilled GitHub issue"]
    share --> issue["you open it and submit<br/>nothing is sent by lxask"]
    fb --> cases["scripts/feedback_cases.py<br/>draft regression cases"]
    cases --> regressions[("evalsets/regressions.jsonl<br/>every later model must pass")]
```

## How the model and the index are made

Offline, on a machine with a GPU (a free Colab T4 or a laptop RTX card):

```mermaid
flowchart TD
    subgraph sources ["sources: scripts/fetch_sources.py"]
        tldr["tldr-pages"]
        nl2bash["NL2Bash"]
        man["man pages of<br/>this machine"]
    end
    tldr & man --> buildindex["build_index.py"] --> docs[("docs.sqlite")]
    tldr --> gen["generate_data.py<br/>requests, scenarios,<br/>execution tasks"]
    gen --> filter["filter_generated.py<br/>validator + reviewer model"]
    filter --> seed[("seed/")]
    seed & tldr & nl2bash --> dataset["build_dataset.py<br/>prompts as the program builds them"]
    dataset --> sft["train_lora.py<br/>LoRA fine-tune of<br/>Qwen2.5-Coder-1.5B"]
    sft --> rl["train_grpo.py<br/>reinforcement learning:<br/>commands run in a sandbox,<br/>rewarded on the result"]
    rl --> export["export_gguf.sh<br/>merge, GGUF, 4-bit"] --> modelgguf[("model")]
    rl --> mix["mix_finetune.py<br/>restrictions and proxy contexts"]
    mix --> adapter["LoRA adapter<br/>convert_lora_to_gguf.py"] --> restrict[("restrict.gguf")]
    modelgguf & restrict & docs --> evals{"eval.py + exec_eval.py<br/>ship rule: no set worse<br/>than before"}
    evals -- pass --> release["build_release.sh<br/>release_files.sh"] --> gh["GitHub release"] --> install["install.sh"]
    evals -- fail --> results["results.md:<br/>tried, not shipped"]
```

The evaluation sets are kept out of training throughout; the execution tests run each
command as an unprivileged user in a container with no network
([development.md](development.md#measuring-it)).

## Files on disk

Everything lives in `~/.local/share/lxask`: the model, the adapter, the encoder, the
index, `last.json` and your `feedback.jsonl`; the program is `~/.local/bin/lxask`.
The full list and every setting: [configuration.md](configuration.md).

## Design choices

- **Check, don't trust.** A 1.5B model gets details wrong, so every command is
  checked against the documentation and the request, retried when a check objects,
  and marked `unverified:` when it still fails. It never runs a command.
- **Small, local, offline.** One program and about 1.1 GB of files, 1.3 GB of memory
  while answering, no network after installing.
- **Route instead of retrain.** The model fine-tuned on restrictions got worse on
  everyday questions, so it ships as an adapter used only for the requests it is
  better at ([results.md](results.md)).
- **Ask back instead of guessing** when a request means different commands in
  different contexts.
- **Fixed text for questions about lxask**, rather than teaching the model facts
  about itself.
- **One core for the program and the measurements.** Training and evaluation call
  the same Rust code through `lxask._core`.
