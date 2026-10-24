//! `lxask "how do I ..."` -> the command for each thing asked, explained from the
//! documentation. Nothing is executed.

mod config;
mod feedback;
mod server;
mod shell;

use std::cell::RefCell;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use serde_json::{json, Value};

use lxask_core::answer::{self, Step};
use lxask_core::prompt::{format_context, Doc, Message, MAX_CONTEXT_CHARS};
use lxask_core::retrieve::{Embed, Retriever, N_COMMANDS, OTHER_EXAMPLES, TOP_EXAMPLES};
use lxask_core::validate::{load_catalog, Catalog};
use lxask_core::Error;
use lxask_llm::{Embedder, Llm};

const USAGE: &str = "usage: lxask [-h] [--about] [--bad] [--share [N]] [--shell-init SHELL] [--model MODEL] [--index INDEX] \
[--no-rag] [--no-explain] [--command-only] [--show-context] [--no-retry] [--threads THREADS] [--serve] [--idle MINUTES] \
[--stop] [--version] [question ...]";

const HELP: &str = "\
Ask how to do something on Linux. Prints commands; never runs them.

positional arguments:
  question           what you want to do

options:
  -h, --help         show this help message and exit
  --about            who made lxask, and where it lives
  --bad             report the last answer as wrong; any words given are kept as a note
                     (lxask --bad should list directories only)
  --share [N]        print the last N reports (default 1) and a link to a prefilled GitHub
                     issue for them; nothing is sent unless you open it and submit
  --model MODEL      GGUF model file (default: $LXASK_MODEL)
  --index INDEX      docs index (default: $LXASK_INDEX)
  --no-rag           answer without retrieved notes
  --no-explain       print the commands without what each part does
  --command-only     print only the command, steps joined by &&; notes go to stderr
  --shell-init SHELL print a key binding for bash or zsh: type a request, press Ctrl+G, and
                     the line becomes the command (eval \"$(lxask --shell-init bash)\")
  --show-context     print the retrieved notes to stderr
  --no-retry         keep the first answer even when the checks object to it (faster)
  --threads THREADS  CPU threads (default: half the logical cores)
  --serve            keep the model loaded and answer later questions quickly (holds about
                     1.4 GB of memory until stopped or idle)
  --idle MINUTES     with --serve: stop after this many minutes without a question (0: never)
                     (with serve = auto in $LXASK_HOME/settings, or LXASK_SERVE=auto, the first
                     question starts the server itself)
  --stop             stop a running server
  --version          show program's version number and exit

A request too vague for one command (\"add a proxy\": for this shell, Git, APT, ...?) gets
a question back instead, naming the choices, and exit status 3.

A request that rules a program out (\"without lsof\") or is about a proxy is answered with
the model's restriction adapter, $LXASK_HOME/restrict.gguf if installed (LXASK_ADAPTER: another
file, or off).";

/// Exit status when lxask asked which thing was meant instead of answering.
const ASKED_BACK: u8 = 3;

#[derive(Default)]
struct Args {
    question: Vec<String>,
    bad: bool,
    model: Option<PathBuf>,
    index: Option<PathBuf>,
    no_rag: bool,
    no_explain: bool,
    show_context: bool,
    no_retry: bool,
    threads: Option<i32>,
    serve: bool,
    idle: f64,
    stop: bool,
    /// just the command on stdout, the rest on stderr (the shell widget)
    command_only: bool,
}

fn usage_error(message: &str) -> ExitCode {
    eprintln!("{USAGE}\nlxask: error: {message}");
    ExitCode::from(2)
}

enum Parsed {
    Run(Args),
    Exit(ExitCode),
}

fn parse(argv: &[String]) -> Parsed {
    let mut args = Args { idle: server::IDLE_MINUTES, ..Args::default() };
    let mut items = argv.iter();
    let mut unknown: Vec<&str> = Vec::new();
    while let Some(arg) = items.next() {
        let (name, inline) = match arg.split_once('=') {
            Some((name, value)) if arg.starts_with("--") => (name, Some(value.to_string())),
            _ => (arg.as_str(), None),
        };
        let mut value = |label: &str| -> Result<String, ExitCode> {
            inline.clone().or_else(|| items.next().cloned()).ok_or_else(|| usage_error(&format!("argument {label}: expected one argument")))
        };
        match name {
            "-h" | "--help" => {
                println!("{USAGE}\n\n{HELP}");
                return Parsed::Exit(ExitCode::SUCCESS);
            }
            "--version" => {
                println!("lxask {}", server::VERSION);
                return Parsed::Exit(ExitCode::SUCCESS);
            }
            "--bad" => args.bad = true,
            "--no-rag" => args.no_rag = true,
            "--no-explain" => args.no_explain = true,
            "--show-context" => args.show_context = true,
            "--no-retry" => args.no_retry = true,
            "--command-only" => args.command_only = true,
            "--serve" => args.serve = true,
            "--stop" => args.stop = true,
            "--model" => match value("--model") {
                Ok(v) => args.model = Some(PathBuf::from(v)),
                Err(code) => return Parsed::Exit(code),
            },
            "--index" => match value("--index") {
                Ok(v) => args.index = Some(PathBuf::from(v)),
                Err(code) => return Parsed::Exit(code),
            },
            "--threads" => match value("--threads").map(|v| v.parse::<i32>().map_err(|_| v)) {
                Ok(Ok(n)) => args.threads = Some(n),
                Ok(Err(v)) => return Parsed::Exit(usage_error(&format!("argument --threads: invalid int value: '{v}'"))),
                Err(code) => return Parsed::Exit(code),
            },
            "--idle" => match value("--idle").map(|v| v.parse::<f64>().map_err(|_| v)) {
                Ok(Ok(n)) => args.idle = n,
                Ok(Err(v)) => return Parsed::Exit(usage_error(&format!("argument --idle: invalid float value: '{v}'"))),
                Err(code) => return Parsed::Exit(code),
            },
            "--" => {
                args.question.extend(items.by_ref().cloned());
            }
            _ if arg.starts_with('-') && arg.len() > 1 && !arg[1..].starts_with(|c: char| c.is_ascii_digit()) => {
                unknown.push(arg)
            }
            _ => args.question.push(arg.clone()),
        }
    }
    if !unknown.is_empty() {
        return Parsed::Exit(usage_error(&format!("unrecognized arguments: {}", unknown.join(" "))));
    }
    Parsed::Run(args)
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().map(String::as_str) == Some("--bad") {
        // Everything after --bad is the note, options included: "should be ls -d */".
        println!("{}", feedback::report(&argv[1..].join(" "), None));
        return ExitCode::SUCCESS;
    }
    if argv.first().map(String::as_str) == Some("--share") {
        let n = match argv.get(1).map(|v| v.parse::<usize>()) {
            None => 1,
            Some(Ok(n)) if argv.len() == 2 => n,
            _ => return usage_error("--share takes how many of the last reports to share, e.g. lxask --share 3"),
        };
        print!("{}", feedback::share(n, None));
        return ExitCode::SUCCESS;
    }
    if argv.first().map(String::as_str) == Some("--about") {
        println!("{}", lxask_core::about::about(server::VERSION));
        return ExitCode::SUCCESS;
    }
    if argv.first().map(String::as_str) == Some("--shell-init") {
        return match argv.get(1).and_then(|shell| shell::init(shell)) {
            Some(script) if argv.len() == 2 => {
                print!("{script}");
                ExitCode::SUCCESS
            }
            _ => usage_error("--shell-init takes bash or zsh, e.g. eval \"$(lxask --shell-init bash)\""),
        };
    }
    match argv.first().map(String::as_str) {
        Some("batch") => return batch(&argv[1..]),
        Some("debug-tokens") => return debug_tokens(&argv[1..]),
        Some("debug-embed") => return debug_embed(&argv[1..]),
        _ => {}
    }
    let args = match parse(&argv) {
        Parsed::Run(args) => args,
        Parsed::Exit(code) => return code,
    };
    let question = args.question.join(" ");
    if args.bad {
        println!("{}", feedback::report(&question, None));
        return ExitCode::SUCCESS;
    }
    if args.stop {
        let stopped = server::request(json!({"type": "stop"}), None, server::ANSWER_TIMEOUT).is_some();
        eprintln!("{}", if stopped { "lxask: server stopped" } else { "lxask: no server is running" });
        return ExitCode::SUCCESS;
    }
    if question.is_empty() && !args.serve {
        return usage_error("say what you want to do, e.g. lxask \"show disk usage of each folder\"");
    }
    // About lxask itself ("who made you?") or too vague for one command ("add a proxy"):
    // answered before loading anything.
    // With --command-only (a shell prompt) the text goes to the terminal, not the line.
    if !args.serve {
        let say = |text: &str| if args.command_only { eprintln!("{text}") } else { println!("{text}") };
        if lxask_core::about::about_itself(&question) {
            say(&lxask_core::about::about(server::VERSION));
            return ExitCode::SUCCESS;
        }
        if let Some(asked) = lxask_core::clarify::clarification(&question) {
            say(&asked);
            return ExitCode::from(ASKED_BACK);
        }
    }

    // A running server has the default model and index loaded; other ones are loaded here.
    // With serve = auto, a question that finds none starts one and asks it.
    if !args.serve && args.model.is_none() && args.index.is_none() && args.threads.is_none() {
        let mut found = ask_server(&question, &args);
        if matches!(found, Server::Missing) && config::serve_auto() && server::start_and_wait(server::START_TIMEOUT) {
            found = ask_server(&question, &args);
        }
        if let Server::Answered(reply) = found {
            let text = |key: &str| reply.get(key).and_then(Value::as_str).unwrap_or("").to_string();
            if !text("stderr").is_empty() {
                eprintln!("{}", text("stderr"));
            }
            if !text("stdout").is_empty() {
                println!("{}", text("stdout"));
            }
            return ExitCode::from(reply.get("code").and_then(Value::as_u64).unwrap_or(0) as u8);
        }
    }

    let model = args.model.clone().unwrap_or_else(config::model_path);
    if !model.exists() {
        eprintln!("lxask: model not found at {} (set LXASK_MODEL or pass --model)", model.display());
        return ExitCode::from(2);
    }
    // the adapter is made for the installed model, not for one passed with --model
    let adapter = if args.model.is_none() { config::adapter_path() } else { None };
    let loaded = match Loaded::load(&model, adapter, args.index.clone(), args.no_rag, args.threads) {
        Ok(loaded) => loaded,
        Err(error) => {
            eprintln!("lxask: {error}");
            return ExitCode::from(1);
        }
    };
    let loaded = RefCell::new(loaded);
    if args.serve {
        let idle = args.idle;
        let ready = |path: &Path| {
            let stops = if idle > 0.0 { format!("; stops after {} min idle", minutes(idle)) } else { String::new() };
            eprintln!("lxask: ready, answering on {}{stops} (lxask --stop)", path.display());
        };
        let code = server::serve(&mut |message| loaded.borrow_mut().respond(message, &model), None, idle, &ready);
        return ExitCode::from(code as u8);
    }
    let reply = loaded.borrow_mut().respond(&message(&question, &args), &model);
    let text = |key: &str| reply.get(key).and_then(Value::as_str).unwrap_or("").to_string();
    if !text("stderr").is_empty() {
        eprintln!("{}", text("stderr"));
    }
    if !text("stdout").is_empty() || !args.command_only {
        println!("{}", text("stdout"));
    }
    ExitCode::from(reply.get("code").and_then(Value::as_u64).unwrap_or(0) as u8)
}

/// With LXASK_TIMING set, how long each start-up stage took, on stderr.
fn timing(stage: &str, since: std::time::Instant) {
    if lxask_core::setting("TIMING").is_some() {
        eprintln!("lxask: {stage} ready after {:.2} s", since.elapsed().as_secs_f64());
    }
}

/// `{:g}` for the idle minutes: 30, 0.5.
fn minutes(value: f64) -> String {
    if value == value.trunc() { format!("{}", value as i64) } else { format!("{value}") }
}

fn message(question: &str, args: &Args) -> Value {
    json!({"type": "ask", "question": question, "explain": !args.no_explain, "retry": !args.no_retry,
           "rag": !args.no_rag, "show_context": args.show_context, "command_only": args.command_only})
}

enum Server {
    Answered(Value),
    /// a server of another version is running: answer here, and do not start another
    Stale,
    Missing,
}

/// The running server's answer, if one is running and of this version.
fn ask_server(question: &str, args: &Args) -> Server {
    let Some(reply) = server::request(message(question, args), None, server::ANSWER_TIMEOUT) else {
        return Server::Missing;
    };
    if let Some(stale) = reply.get("stale") {
        eprintln!(
            "lxask: the running server is lxask {}, this is {}; answering here (restart it: lxask --stop; lxask --serve)",
            stale.as_str().unwrap_or("?"),
            server::VERSION
        );
        return Server::Stale;
    }
    Server::Answered(reply)
}

/// The model, the retriever and the catalogue, whichever the index and the flags allow.
struct Loaded {
    llm: RefCell<Llm>,
    retriever: Option<Retriever>,
    catalog: Option<Catalog>,
}

impl Loaded {
    fn load(model: &Path, adapter: Option<PathBuf>, index: Option<PathBuf>, no_rag: bool, threads: Option<i32>)
        -> Result<Loaded, Error> {
        // Loading the model is most of the start-up time, so it goes on in a thread
        // while the catalogue, the encoder and the index load here.
        let started = std::time::Instant::now();
        let model_path = model.to_path_buf();
        let loading = std::thread::spawn(move || {
            let weights = lxask_llm::preload(&model_path, adapter.as_deref());
            timing("model weights", started);
            weights
        });
        let index = index.unwrap_or_else(config::index_path);
        let mut retriever = None;
        let mut catalog = None;
        if index.exists() {
            catalog = load_catalog(&index);
            if catalog.is_none() {
                eprintln!("lxask: this index has no catalogue; commands are not checked or explained");
            }
            if !no_rag {
                let embed_path = config::embed_model_path();
                let embed: Option<Embed> = if embed_path.exists() {
                    let mut embedder = Embedder::load(&embed_path, threads)?;
                    Some(Box::new(move |texts: &[String]| embedder.embed(texts)))
                } else {
                    None
                };
                retriever = Some(Retriever::new(&index, embed, true)?);
            }
        } else {
            eprintln!("lxask: no docs index at {}, answering from the model alone", index.display());
        }
        timing("catalogue, encoder, index", started);
        let weights = loading.join().map_err(|_| "loading the model failed")??;
        let llm = Llm::with_model(weights, 2048, threads)?;
        timing("context", started);
        Ok(Loaded { llm: RefCell::new(llm), retriever, catalog })
    }

    fn answer(&mut self, question: &str, rag: bool, retry: bool) -> Result<Vec<Step>, Error> {
        let llm = &self.llm;
        llm.borrow_mut().specialize(lxask_core::route::specialist(question, self.catalog.as_ref()))?;
        let mut chat = |m: &[Message]| llm.borrow_mut().ask(m);
        let mut resample = |m: &[Message], attempt: usize| llm.borrow_mut().sample(m, attempt);
        let retriever = &mut self.retriever;
        let mut search = |task: &str| -> Result<Vec<Doc>, Error> {
            match retriever.as_mut() {
                Some(r) if rag => r.search(task, N_COMMANDS, TOP_EXAMPLES, OTHER_EXAMPLES),
                _ => Ok(Vec::new()),
            }
        };
        answer::answer(
            question,
            &mut chat,
            &mut search,
            self.catalog.as_ref(),
            if retry { Some(&mut resample as &mut answer::Resample) } else { None },
        )
    }

    /// One question answered, as printed: the same here and in the server.
    fn respond(&mut self, message: &Value, model: &Path) -> Value {
        let flag = |key: &str| message.get(key).and_then(Value::as_bool).unwrap_or(true);
        let question = message.get("question").and_then(Value::as_str).unwrap_or("");
        let started = std::time::Instant::now();
        let answered = self.answer(question, flag("rag"), flag("retry"));
        timing("answer", started);
        let steps = match answered {
            Ok(steps) => steps,
            Err(error) => return json!({"stdout": "", "stderr": format!("lxask: {error}"), "code": 1}),
        };
        let mut stderr = String::new();
        if message.get("show_context").and_then(Value::as_bool).unwrap_or(false) {
            stderr = steps
                .iter()
                .map(|s| format!("notes for: {}\n{}\n", s.task, format_context(&s.notes, MAX_CONTEXT_CHARS)))
                .collect::<Vec<_>>()
                .join("\n");
        }
        let stdout = if message.get("command_only").and_then(Value::as_bool).unwrap_or(false) {
            let (line, notes) = answer::render_command_line(&steps, question);
            if !notes.is_empty() {
                stderr = if stderr.is_empty() { notes } else { format!("{stderr}\n{notes}") };
            }
            line
        } else {
            answer::render(&steps, flag("explain"), Some(question))
        };
        feedback::remember(question, &steps, model, None);
        json!({"stdout": stdout, "stderr": stderr, "code": 0})
    }
}

fn jsonl_lines(path: &str) -> Vec<Value> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}

/// `lxask debug-tokens MODEL IN.jsonl`: the prompt token ids for each {"messages": [...]}
/// line, one JSON list per line on stdout (for comparing with llama-cpp-python).
fn debug_tokens(argv: &[String]) -> ExitCode {
    let [model, input] = argv else { return ExitCode::from(2) };
    let llm = match Llm::load(Path::new(model), 2048, None) {
        Ok(llm) => llm,
        Err(error) => {
            eprintln!("lxask: {error}");
            return ExitCode::from(1);
        }
    };
    for record in jsonl_lines(input) {
        let messages: Vec<Message> = record["messages"]
            .as_array()
            .map(|list| {
                list.iter()
                    .map(|m| Message {
                        role: if m["role"] == "system" { "system" } else if m["role"] == "user" { "user" } else { "assistant" },
                        content: m["content"].as_str().unwrap_or("").to_string(),
                    })
                    .collect()
            })
            .unwrap_or_default();
        println!("{}", json!(llm.tokenize_prompt(&messages)));
    }
    ExitCode::SUCCESS
}

/// `lxask debug-embed IN.jsonl`: the query embedding for each {"text": ...} line.
fn debug_embed(argv: &[String]) -> ExitCode {
    let [input] = argv else { return ExitCode::from(2) };
    let mut embedder = match Embedder::load(&config::embed_model_path(), None) {
        Ok(embedder) => embedder,
        Err(error) => {
            eprintln!("lxask: {error}");
            return ExitCode::from(1);
        }
    };
    for record in jsonl_lines(input) {
        let text = record["text"].as_str().unwrap_or("").to_string();
        match embedder.embed(&[text]) {
            Ok(vectors) => println!("{}", json!(vectors[0])),
            Err(error) => println!("{}", json!({"error": error.to_string()})),
        }
    }
    ExitCode::SUCCESS
}

/// `lxask batch IN.jsonl OUT.jsonl`: answer every {"q": ...} line, for evaluating the
/// binary itself (scripts/eval.py --engine rust). Not in --help.
fn batch(argv: &[String]) -> ExitCode {
    let mut paths = Vec::new();
    let mut model = config::model_path();
    let mut adapter = config::adapter_path();
    let mut index = None;
    let mut retry = true;
    let mut rag = true;
    let mut threads = None;
    let mut items = argv.iter();
    while let Some(arg) = items.next() {
        match arg.as_str() {
            "--model" => model = items.next().map(PathBuf::from).unwrap_or(model),
            "--adapter" => adapter = items.next().map(PathBuf::from),
            "--no-adapter" => adapter = None,
            "--index" => index = items.next().map(PathBuf::from),
            "--threads" => threads = items.next().and_then(|n| n.parse().ok()),
            "--no-retry" => retry = false,
            "--no-rag" => rag = false,
            _ => paths.push(PathBuf::from(arg)),
        }
    }
    let [input, output] = &paths[..] else {
        eprintln!("usage: lxask batch [--model M] [--adapter A | --no-adapter] [--index I] [--threads N] [--no-retry] \
                   [--no-rag] IN.jsonl OUT.jsonl");
        return ExitCode::from(2);
    };
    let mut loaded = match Loaded::load(&model, adapter, index, !rag, threads) {
        Ok(loaded) => loaded,
        Err(error) => {
            eprintln!("lxask: {error}");
            return ExitCode::from(1);
        }
    };
    let (Ok(reader), Ok(mut out)) = (std::fs::File::open(input), std::fs::File::create(output)) else {
        eprintln!("lxask: cannot open {} or {}", input.display(), output.display());
        return ExitCode::from(1);
    };
    for line in std::io::BufReader::new(reader).lines().map_while(Result::ok) {
        let Ok(record) = serde_json::from_str::<Value>(&line) else { continue };
        let question = record.get("q").and_then(Value::as_str).unwrap_or("").to_string();
        let started = std::time::Instant::now();
        if lxask_core::about::about_itself(&question) {
            let about = lxask_core::about::about(server::VERSION);
            if writeln!(out, "{}", json!({"q": question, "steps": [], "about": about, "rendered": about, "seconds": 0.0})).is_err() {
                return ExitCode::from(1);
            }
            continue;
        }
        if let Some(asked) = lxask_core::clarify::clarification(&question) {
            if writeln!(out, "{}", json!({"q": question, "steps": [], "clarify": asked, "rendered": asked, "seconds": 0.0})).is_err() {
                return ExitCode::from(1);
            }
            continue;
        }
        let result = match loaded.answer(&question, rag, retry) {
            Ok(steps) => json!({
                "q": question,
                "steps": steps.iter().map(|s| json!({"task": s.task, "command": s.command, "issues": s.issues,
                                                     "misses": s.misses, "tries": s.tries})).collect::<Vec<_>>(),
                "rendered": answer::render(&steps, true, Some(&question)),
                "seconds": started.elapsed().as_secs_f64(),
            }),
            Err(error) => json!({"q": question, "error": error.to_string()}),
        };
        if writeln!(out, "{result}").is_err() {
            return ExitCode::from(1);
        }
    }
    ExitCode::SUCCESS
}
