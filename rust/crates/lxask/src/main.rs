//! `lxask "how do I ..."` -> the command for each thing asked, explained from the
//! documentation. Nothing is executed.

mod config;
mod feedback;
mod server;
mod shell;
mod update;

use std::cell::RefCell;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use serde_json::{json, Value};

use lxask_core::answer::{self, Step};
use lxask_core::prompt::{format_context, Doc, Message, MAX_CONTEXT_CHARS};
use lxask_core::retrieve::{Embed, Retriever, N_COMMANDS, OTHER_EXAMPLES, TOP_EXAMPLES};
use lxask_core::validate::Catalog;
use lxask_core::Error;
use lxask_llm::{Embedder, Llm};

const USAGE: &str = "usage: lxask [-h] [--about] [--explain COMMAND ...] [--bad] [--share [N]] [--shell-init SHELL] [--model MODEL] [--index INDEX] \
[--no-rag] [--no-explain] [--command-only] [--show-context] [--no-retry] [--no-verify] [--threads THREADS] [--serve] [--listen HOST:PORT] \
[--idle MINUTES] [--stop] [--upgrade] [--version] [question ...]";

const HELP: &str = "\
Ask how to do something on Linux. Prints commands; never runs them.

positional arguments:
  question           what you want to do

options:
  -h, --help         show this help message and exit
  --about            who made lxask, and where it lives
  --explain COMMAND  explain a command you already have, part by part, from the documentation,
                     and check it the way answers are checked; no model is loaded
                     (lxask --explain \"tar -xzvf logs.tar.gz\")
  --bad             report the last answer as wrong; any words given are kept as a note
                     (lxask --bad should list directories only)
  --share [N]        print the last N reports (default 1) and a link to a prefilled GitHub
                     issue for them; nothing is sent unless you open it and submit
  --model MODEL      GGUF model file (default: $LXASK_MODEL)
  --index INDEX      docs index (default: $LXASK_INDEX)
  --no-rag           answer without retrieved notes
  --no-explain       print the commands without what each part does
  --command-only     print only the command, steps joined by &&; notes go to stderr, and a
                     command with an unverified: or a warning: comes commented out (# )
  --shell-init SHELL print a key binding for bash, zsh or fish: type a request, press Ctrl+G, and
                     the line becomes the command (eval \"$(lxask --shell-init bash)\";
                     fish: lxask --shell-init fish | source)
  --show-context     print the retrieved notes to stderr
  --no-retry         keep the first answer even when the checks object to it (faster)
  --no-verify        skip the model's second look at whether an answer does what was asked
                     (faster; `verify = off` in $LXASK_HOME/settings, or LXASK_VERIFY=off,
                     turns the look off everywhere)
  --threads THREADS  CPU threads (default: one per core)
  --serve            keep the model loaded and answer later questions quickly (holds about
                     1.4 GB of memory until stopped or idle)
  --idle MINUTES     with --serve: stop after this many minutes without a question (0: never;
                     default: idle = in $LXASK_HOME/settings, else 30)
                     (with serve = auto in $LXASK_HOME/settings, or LXASK_SERVE=auto, the first
                     question starts the server itself)
  --listen HOST:PORT with --serve: also answer other machines over TCP (lxask --serve --listen
                     0.0.0.0:7871); each request needs the token in $LXASK_HOME/token, made on
                     first use. Never stops when idle unless --idle is given. Elsewhere, with only
                     this program installed: LXASK_SERVER=host:7871 LXASK_TOKEN=... lxask \"...\"
                     (or server = and token = in $LXASK_HOME/settings)
  --stop             stop a running server
  --upgrade          install the newest release: fetches its install.sh and runs it
  --version          show program's version number and exit

A request too vague for one command (\"add a proxy\": for this shell, Git, APT, ...?) gets
a question back instead, naming the choices, and exit status 3.

A request that rules a program out (\"without lsof\") or is about a proxy is answered with
the model's restriction adapter, $LXASK_HOME/restrict.gguf if installed (LXASK_ADAPTER: another
file, or off); other requests get the precision adapter, precision.gguf (LXASK_PRECISION:
another file, or off).";

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
    /// skip the second look at answers that pass the checks
    no_verify: bool,
    threads: Option<i32>,
    serve: bool,
    /// with --serve: also answer over TCP at this host:port
    listen: Option<String>,
    idle: f64,
    idle_given: bool,
    stop: bool,
    /// just the command on stdout, the rest on stderr (the shell widget)
    command_only: bool,
    /// the detached worker of the release check (update::maybe_spawn_detached)
    update_check: bool,
    /// fetch the newest release's install.sh and run it (update::upgrade)
    upgrade: bool,
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
            "--no-verify" => args.no_verify = true,
            "--command-only" => args.command_only = true,
            "--serve" => args.serve = true,
            "--stop" => args.stop = true,
            "--update-check" => args.update_check = true,
            "--upgrade" => args.upgrade = true,
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
            "--listen" => match value("--listen") {
                Ok(v) => args.listen = Some(v),
                Err(code) => return Parsed::Exit(code),
            },
            "--idle" => match value("--idle").map(|v| v.parse::<f64>().map_err(|_| v)) {
                Ok(Ok(n)) => {
                    args.idle = n;
                    args.idle_given = true;
                }
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
    if argv.first().map(String::as_str) == Some("--explain") {
        // Everything after --explain is the command, its options included.
        return explain_command(&argv[1..].join(" "));
    }
    if argv.first().map(String::as_str) == Some("--shell-init") {
        return match argv.get(1).and_then(|shell| shell::init(shell)) {
            Some(script) if argv.len() == 2 => {
                print!("{script}");
                ExitCode::SUCCESS
            }
            _ => usage_error("--shell-init takes bash, zsh or fish, e.g. eval \"$(lxask --shell-init bash)\""),
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
        return match server::request(json!({"type": "stop"}), None, server::ANSWER_TIMEOUT) {
            Ok(_) => {
                eprintln!("lxask: server stopped");
                ExitCode::SUCCESS
            }
            Err(server::Failure::NoServer) => {
                eprintln!("lxask: no server is running");
                ExitCode::SUCCESS
            }
            Err(server::Failure::Broken(reason)) => {
                eprintln!("lxask: the running server did not answer ({reason}); it may be busy with a question");
                ExitCode::from(1)
            }
        };
    }
    if args.update_check {
        // a detached copy of this program does the day's release check (update.rs)
        update::run_and_write();
        return ExitCode::SUCCESS;
    }
    if args.upgrade {
        return ExitCode::from(update::upgrade());
    }
    if question.is_empty() && !args.serve {
        return usage_error("say what you want to do, e.g. lxask \"show disk usage of each folder\"");
    }
    if args.listen.is_some() && !args.serve {
        return usage_error("--listen goes with --serve, e.g. lxask --serve --listen 0.0.0.0:7871");
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

    // A real question: a day-stale release check starts apart from the answer
    // (the note, when there is a newer release, follows the answer — update.rs).
    if !args.serve {
        update::maybe_spawn_detached();
    }

    // Another machine's server (LXASK_SERVER), when set, answers everything: nothing
    // but this program needs to be installed here.
    if !args.serve && args.model.is_none() && args.index.is_none() {
        if let Some(address) = config::remote_server() {
            return ask_remote(&address, message(&question, &args), args.command_only);
        }
    }

    // With --listen, take the port and the token before the slow load, so a mistake
    // shows at once.
    let (remote, token_source) = match args.listen.as_deref() {
        Some(address) => {
            let (token, source) = match server::ensure_token() {
                Ok(found) => found,
                Err(error) => {
                    eprintln!("lxask: {error}");
                    return ExitCode::from(1);
                }
            };
            match std::net::TcpListener::bind(address) {
                Ok(listener) => (Some(server::Remote { listener, token }), source),
                Err(error) => {
                    eprintln!("lxask: cannot listen on {address}: {error}");
                    return ExitCode::from(1);
                }
            }
        }
        None => (None, String::new()),
    };

    // A running server has the default model and index loaded; other ones are loaded here.
    // With serve = auto, a question that finds none starts one and asks it.
    let mut broken = None;
    if !args.serve && args.model.is_none() && args.index.is_none() && args.threads.is_none() {
        let mut found = ask_server(&question, &args);
        if matches!(found, Server::Missing) && config::serve_auto() {
            // the server it starts has no terminal to say this on
            if let Some(Err(text)) = config::idle_minutes() {
                eprintln!(
                    "lxask: idle = {text} is not a number of minutes; stopping after {} min idle",
                    minutes(server::IDLE_MINUTES)
                );
            }
            eprintln!("lxask: starting up; loading the model makes the first answer slower");
            if server::start_and_wait(server::START_TIMEOUT) {
                found = ask_server(&question, &args);
            }
        }
        if let Server::Broken(reason) = found {
            broken = Some(reason);
        } else if let Server::Answered(reply) = found {
            let text = |key: &str| reply.get(key).and_then(Value::as_str).unwrap_or("").to_string();
            if !text("stderr").is_empty() {
                eprintln!("{}", text("stderr"));
            }
            if !text("stdout").is_empty() {
                println!("{}", text("stdout"));
            }
            say_update();
            return ExitCode::from(reply.get("code").and_then(Value::as_u64).unwrap_or(0) as u8);
        }
    }

    let model = args.model.clone().unwrap_or_else(config::model_path);
    if !model.exists() {
        match &broken {
            Some(reason) => eprintln!(
                "lxask: the running server did not answer ({reason}), and there is no model at {} to answer here",
                model.display()
            ),
            None => eprintln!("lxask: model not found at {} (set LXASK_MODEL or pass --model)", model.display()),
        }
        return ExitCode::from(2);
    }
    if let Some(reason) = &broken {
        eprintln!("lxask: the running server did not answer ({reason}); answering here");
    }
    // the adapters are made for the installed model, not for one passed with --model
    let (restrict, precision) =
        if args.model.is_none() { (config::adapter_path(), config::precision_adapter_path()) } else { (None, None) };
    eprintln!("lxask: loading the model, this takes a few seconds");
    let loaded = match Loaded::load(&model, restrict, precision, args.index.clone(), args.no_rag, args.threads) {
        Ok(loaded) => loaded,
        Err(error) => {
            eprintln!("lxask: {error}");
            return ExitCode::from(1);
        }
    };
    let loaded = RefCell::new(loaded);
    if args.serve {
        // --idle, else (for a server only this user asks) `idle =` in the settings; a
        // server other machines ask is not stopped by an idle spell unless --idle says so
        let idle = if args.idle_given {
            args.idle
        } else if remote.is_some() {
            0.0
        } else {
            match config::idle_minutes() {
                Some(Ok(minutes)) => minutes,
                Some(Err(text)) => {
                    eprintln!(
                        "lxask: idle = {text} is not a number of minutes; stopping after {} min idle",
                        minutes(args.idle)
                    );
                    args.idle
                }
                None => args.idle,
            }
        };
        let network = match (&remote, args.listen.as_deref()) {
            (Some(remote), Some(address)) => {
                let at = remote.listener.local_addr().map(|a| a.to_string()).unwrap_or_else(|_| address.to_string());
                format!(" and on {at} (clients need the token, from {token_source})")
            }
            _ => String::new(),
        };
        let ready = |path: &Path| {
            let stops = if idle > 0.0 { format!("; stops after {} min idle", minutes(idle)) } else { String::new() };
            eprintln!("lxask: ready, answering on {}{network}{stops} (lxask --stop)", path.display());
        };
        update::maybe_spawn_thread();
        let code =
            server::serve(&mut |message| loaded.borrow_mut().respond(message, &model), None, remote.as_ref(), idle, &ready);
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
    say_update();
    ExitCode::from(reply.get("code").and_then(Value::as_u64).unwrap_or(0) as u8)
}

/// "a newer release is out", when the day's background check saw one (update.rs).
fn say_update() {
    if let Some(note) = update::notice() {
        eprintln!("{note}");
    }
}

/// `lxask --explain COMMAND`: the command checked and described from the docs index
/// alone, as an answer's command would be. Fast, since no model is loaded.
fn explain_command(command: &str) -> ExitCode {
    if command.trim().is_empty() {
        return usage_error("--explain takes a command, e.g. lxask --explain \"tar -xzvf logs.tar.gz\"");
    }
    // with LXASK_SERVER the server's index explains it, so none is needed here
    if let Some(address) = config::remote_server() {
        return ask_remote(&address, json!({"type": "explain", "command": command}), false);
    }
    let index = config::index_path();
    if !index.exists() {
        eprintln!("lxask: no docs index at {} (set LXASK_INDEX, or run install.sh)", index.display());
        return ExitCode::from(2);
    }
    let Some(catalog) = config::catalog(&index) else {
        eprintln!("lxask: the index at {} has no catalogue to explain from", index.display());
        return ExitCode::from(1);
    };
    println!("{}", answer::render(&[answer::describe(command, Some(&catalog))], true, None));
    ExitCode::SUCCESS
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
           "verify": !args.no_verify, "rag": !args.no_rag, "show_context": args.show_context, "command_only": args.command_only})
}

enum Server {
    Answered(Value),
    /// a server of another version is running: answer here, and do not start another
    Stale,
    Missing,
    /// one is there and did not answer (why, for the user): answer here instead
    Broken(String),
}

/// Ask the server at `address` (LXASK_SERVER) and print its reply as a local answer
/// would be printed.
fn ask_remote(address: &str, message: Value, command_only: bool) -> ExitCode {
    let Some(token) = config::token() else {
        eprintln!(
            "lxask: LXASK_SERVER is {address}, but there is no token for it \
             (set LXASK_TOKEN, `token =` in the settings, or {})",
            config::token_path().display()
        );
        return ExitCode::from(2);
    };
    let message_question =
        (message.get("type").and_then(Value::as_str) == Some("ask")).then(|| message["question"].as_str().unwrap_or("").to_string());
    let reply = match server::request_remote(address, &token, message, server::ANSWER_TIMEOUT) {
        Ok(reply) => reply,
        Err(error) => {
            eprintln!("lxask: {error} (LXASK_SERVER)");
            return ExitCode::from(1);
        }
    };
    if let Some(stale) = reply.get("stale") {
        eprintln!(
            "lxask: the server at {address} is lxask {}, this is {}; install the same version on both",
            stale.as_str().unwrap_or("?"),
            server::VERSION
        );
        return ExitCode::from(1);
    }
    let text = |key: &str| reply.get(key).and_then(Value::as_str).unwrap_or("").to_string();
    let code = reply.get("code").and_then(Value::as_u64).unwrap_or(0) as u8;
    // kept here, so `lxask --bad` on this machine reports this answer
    if let (Some(question), Some(steps)) =
        (message_question.as_deref(), reply.get("steps").and_then(Value::as_array))
    {
        let steps: Vec<Step> = steps
            .iter()
            .map(|s| Step {
                command: s.get("command").and_then(Value::as_str).unwrap_or("").to_string(),
                ..Step::new(s.get("task").and_then(Value::as_str).unwrap_or(""))
            })
            .collect();
        feedback::remember(question, &steps, Path::new(&text("model")), None);
    }
    if !text("stderr").is_empty() {
        eprintln!("{}", text("stderr"));
    }
    // an empty answer is still a line, as a local one is; a refusal is only its message
    if !text("stdout").is_empty() || (!command_only && code == 0) {
        println!("{}", text("stdout"));
    }
    say_update();
    ExitCode::from(code)
}

/// The running server's answer, if one is running and of this version.
fn ask_server(question: &str, args: &Args) -> Server {
    let reply = match server::request(message(question, args), None, server::ANSWER_TIMEOUT) {
        Ok(reply) => reply,
        Err(server::Failure::NoServer) => return Server::Missing,
        Err(server::Failure::Broken(reason)) => return Server::Broken(reason),
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
    fn load(model: &Path, restrict: Option<PathBuf>, precision: Option<PathBuf>, index: Option<PathBuf>,
            no_rag: bool, threads: Option<i32>) -> Result<Loaded, Error> {
        // Loading the model is most of the start-up time, so it goes on in a thread
        // while the catalogue, the encoder and the index load here.
        let started = std::time::Instant::now();
        let model_path = model.to_path_buf();
        let loading = std::thread::spawn(move || {
            let weights = lxask_llm::preload(&model_path, restrict.as_deref(), precision.as_deref());
            timing("model weights", started);
            weights
        });
        let index = index.unwrap_or_else(config::index_path);
        let mut retriever = None;
        let mut catalog = None;
        if index.exists() {
            catalog = config::catalog(&index);
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
        timing(&format!("context ({} threads)", threads.unwrap_or_else(lxask_llm::default_threads)), started);
        Ok(Loaded { llm: RefCell::new(llm), retriever, catalog })
    }

    fn answer(&mut self, question: &str, rag: bool, retry: bool, verify: bool) -> Result<Vec<Step>, Error> {
        let llm = &self.llm;
        let routed = lxask_core::route::adapter(question, self.catalog.as_ref());
        llm.borrow_mut().specialize(routed)?;
        // LXASK_TIMING: where the time went, as (calls, time) for the model and for retrieval
        let model_time = std::cell::Cell::new((0u32, std::time::Duration::ZERO));
        let search_time = std::cell::Cell::new((0u32, std::time::Duration::ZERO));
        let tick = |cell: &std::cell::Cell<(u32, std::time::Duration)>, since: std::time::Instant| {
            let (n, t) = cell.get();
            cell.set((n + 1, t + since.elapsed()));
        };
        let mut chat = |m: &[Message]| {
            let started = std::time::Instant::now();
            let mut b = llm.borrow_mut();
            let reply = b.specialize(routed).and_then(|_| b.ask(m));
            tick(&model_time, started);
            reply
        };
        let mut resample = |m: &[Message], attempt: usize| {
            let started = std::time::Instant::now();
            let mut b = llm.borrow_mut();
            let reply = b.specialize(routed).and_then(|_| b.sample(m, attempt));
            tick(&model_time, started);
            reply
        };
        // The second look judges under the precision adapter whichever way the request
        // routed: the restriction adapter is trained to push a program aside, and it
        // reads doubt into answers that obey the push.
        let mut verify_chat = |m: &[Message]| {
            let started = std::time::Instant::now();
            let mut b = llm.borrow_mut();
            let reply = b.specialize(lxask_core::route::Adapter::Precision).and_then(|_| b.ask(m));
            tick(&model_time, started);
            reply
        };
        let retriever = &mut self.retriever;
        let mut search = |task: &str| -> Result<Vec<Doc>, Error> {
            let started = std::time::Instant::now();
            let found = match retriever.as_mut() {
                Some(r) if rag => r.search(task, N_COMMANDS, TOP_EXAMPLES, OTHER_EXAMPLES),
                _ => Ok(Vec::new()),
            };
            tick(&search_time, started);
            found
        };
        let steps = answer::answer(
            question,
            &mut chat,
            &mut search,
            self.catalog.as_ref(),
            if retry { Some(&mut resample as &mut answer::Resample) } else { None },
            if verify { Some(&mut verify_chat as &mut answer::Chat) } else { None },
        );
        let stats = llm.borrow_mut().take_stats(); // taken every time, so each answer counts its own
        if lxask_core::setting("TIMING").is_some() {
            let ((m, mt), (s, st)) = (model_time.get(), search_time.get());
            eprintln!("lxask: model {m} calls {:.2} s (prompt {} tokens {:.2} s, reply {} tokens {:.2} s), retrieval {s} calls {:.2} s",
                      mt.as_secs_f64(), stats.prompt_tokens, stats.prompt_secs, stats.reply_tokens, stats.reply_secs, st.as_secs_f64());
        }
        steps
    }

    /// The second look on a command given directly (`{"q": ..., "cmd": ...}` batch
    /// records), so the look can be measured on commands chosen ahead, not generated.
    fn look(&mut self, question: &str, command: &str, rag: bool) -> Result<Value, Error> {
        let llm = &self.llm;
        llm.borrow_mut().specialize(lxask_core::route::Adapter::Precision)?;
        let Some(catalog) = self.catalog.as_ref() else {
            return Ok(json!({"verdict": "skipped", "reason": "no catalogue"}));
        };
        let notes = match self.retriever.as_mut() {
            Some(r) if rag => r.search(question, N_COMMANDS, TOP_EXAMPLES, OTHER_EXAMPLES)?,
            _ => Vec::new(),
        };
        let described = lxask_core::explain::explain(command, catalog);
        let ruled = lxask_core::constraints::forbidden(question);
        let messages = lxask_core::verify::messages(question, command, &notes, &described, &ruled);
        let reply = llm.borrow_mut().ask(&messages)?.unwrap_or_default();
        let verdict = match lxask_core::verify::verdict(&reply) {
            lxask_core::verify::Verdict::Ok => "ok",
            lxask_core::verify::Verdict::Wrong(_) => "wrong",
            lxask_core::verify::Verdict::Unknown => "unknown",
        };
        let objection = lxask_core::verify::objection(&reply, command, question);
        Ok(json!({"verdict": verdict, "objection": objection, "raw": reply}))
    }

    /// One question answered, as printed: the same here and in the server. An
    /// "explain" request (lxask --explain through a server) is described from the index.
    fn respond(&mut self, message: &Value, model: &Path) -> Value {
        if message.get("type").and_then(Value::as_str) == Some("explain") {
            let command = message.get("command").and_then(Value::as_str).unwrap_or("");
            if command.trim().is_empty() {
                return json!({"stdout": "", "stderr": "lxask: --explain takes a command", "code": 2});
            }
            let described = answer::describe(command, self.catalog.as_ref());
            return json!({"stdout": answer::render(&[described], true, None), "stderr": "", "code": 0});
        }
        let flag = |key: &str| message.get(key).and_then(Value::as_bool).unwrap_or(true);
        let question = message.get("question").and_then(Value::as_str).unwrap_or("");
        let started = std::time::Instant::now();
        let answered = self.answer(question, flag("rag"), flag("retry"), flag("verify") && config::verify());
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
        // A question from another machine (lxask --serve --listen) is remembered there, from
        // the steps in the reply, so that its `lxask --bad` reports it and this one's does not.
        if message.get("network").and_then(Value::as_bool) != Some(true) {
            feedback::remember(question, &steps, model, None);
        }
        let asked: Vec<Value> = steps.iter().map(|s| json!({"task": s.task, "command": s.command})).collect();
        json!({"stdout": stdout, "stderr": stderr, "code": 0, "steps": asked,
               "model": feedback::file_name(model)})
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
/// binary itself (scripts/eval.py --engine rust). A {"q": ..., "cmd": ...} line skips
/// answering and gives only the second look's verdict on that command. Not in --help.
fn batch(argv: &[String]) -> ExitCode {
    let mut paths = Vec::new();
    let mut model = config::model_path();
    let mut restrict = config::adapter_path();
    let mut precision = config::precision_adapter_path();
    let mut index = None;
    let mut retry = true;
    let mut rag = true;
    let mut verify = config::verify();
    let mut threads = None;
    let mut items = argv.iter();
    while let Some(arg) = items.next() {
        match arg.as_str() {
            "--model" => model = items.next().map(PathBuf::from).unwrap_or(model),
            "--adapter" => restrict = items.next().map(PathBuf::from),
            "--precision" => precision = items.next().map(PathBuf::from),
            "--no-adapter" => {
                restrict = None;
                precision = None;
            }
            "--index" => index = items.next().map(PathBuf::from),
            "--threads" => threads = items.next().and_then(|n| n.parse().ok()),
            "--no-retry" => retry = false,
            "--no-rag" => rag = false,
            "--verify" => verify = true,
            "--no-verify" => verify = false,
            _ => paths.push(PathBuf::from(arg)),
        }
    }
    let [input, output] = &paths[..] else {
        eprintln!("usage: lxask batch [--model M] [--adapter A] [--precision P] [--no-adapter] [--index I] [--threads N] \
                   [--no-retry] [--no-rag] [--verify|--no-verify] IN.jsonl OUT.jsonl");
        return ExitCode::from(2);
    };
    let mut loaded = match Loaded::load(&model, restrict, precision, index, !rag, threads) {
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
        if let Some(command) = record.get("cmd").and_then(Value::as_str) {
            let result = match loaded.look(&question, command, rag) {
                Ok(mut looked) => {
                    looked["q"] = json!(question);
                    looked["cmd"] = json!(command);
                    looked["seconds"] = json!(started.elapsed().as_secs_f64());
                    looked
                }
                Err(error) => json!({"q": question, "cmd": command, "error": error.to_string()}),
            };
            if writeln!(out, "{result}").is_err() {
                return ExitCode::from(1);
            }
            continue;
        }
        let result = match loaded.answer(&question, rag, retry, verify) {
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
