//! `lxask._core`: lxask-core for the Python package, so training, evaluation and the
//! Python CLI run the same rules as the `lxask` binary. The Python modules (lxask.prompt,
//! lxask.constraints, ...) wrap these functions in their dataclasses.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PyTuple};

use lxask_core::answer::{self, Step};
use lxask_core::constraints::{self, Num, Wanted};
use lxask_core::explain::{self, Described};
use lxask_core::prompt::{self, Doc, Message};
use lxask_core::retrieve;
use lxask_core::validate;
use lxask_core::{looped, safety, split, Error};

// conversions

fn to_py_err(error: Error) -> PyErr {
    match error.downcast::<PyErr>() {
        Ok(error) => *error,
        Err(error) => PyRuntimeError::new_err(error.to_string()),
    }
}

/// A lxask Doc from any object with cmd, title, body, source and score.
fn doc_from(obj: &Bound<'_, PyAny>) -> PyResult<Doc> {
    Ok(Doc {
        cmd: obj.getattr("cmd")?.extract()?,
        title: obj.getattr("title")?.extract()?,
        body: obj.getattr("body")?.extract()?,
        source: obj.getattr("source")?.extract()?,
        score: obj.getattr("score")?.extract()?,
    })
}

fn docs_from(docs: &Bound<'_, PyAny>) -> PyResult<Vec<Doc>> {
    docs.try_iter()?.map(|doc| doc_from(&doc?)).collect()
}

type DocTuple = (String, String, String, String, f64);

fn doc_tuple(doc: Doc) -> DocTuple {
    (doc.cmd, doc.title, doc.body, doc.source, doc.score)
}

fn messages_list<'py>(py: Python<'py>, messages: &[Message]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for message in messages {
        let dict = PyDict::new(py);
        dict.set_item("role", message.role)?;
        dict.set_item("content", &message.content)?;
        list.append(dict)?;
    }
    Ok(list)
}

/// A Python model callable (messages -> str | None) as the flow's `Chat`.
fn chat_of<'a, 'py>(py: Python<'py>, chat: &'a Bound<'py, PyAny>) -> impl FnMut(&[Message]) -> Result<Option<String>, Error> + 'a
where
    'py: 'a,
{
    move |messages: &[Message]| {
        let reply = chat.call1((messages_list(py, messages)?,))?;
        Ok(reply.extract::<Option<String>>()?)
    }
}

fn resample_of<'a, 'py>(
    py: Python<'py>,
    resample: &'a Bound<'py, PyAny>,
) -> impl FnMut(&[Message], usize) -> Result<Option<String>, Error> + 'a
where
    'py: 'a,
{
    move |messages: &[Message], attempt: usize| {
        let reply = resample.call1((messages_list(py, messages)?, attempt))?;
        Ok(reply.extract::<Option<String>>()?)
    }
}

type DescribedTuple = (String, String, Vec<(String, String)>);

fn described_tuple(entry: Described) -> DescribedTuple {
    (entry.program, entry.summary, entry.options)
}

fn described_from(obj: &Bound<'_, PyAny>) -> PyResult<Described> {
    let options: Vec<(String, String)> =
        obj.getattr("options")?.try_iter()?.map(|o| o?.extract()).collect::<PyResult<_>>()?;
    Ok(Described { program: obj.getattr("program")?.extract()?, summary: obj.getattr("summary")?.extract()?, options })
}

type StepTuple = (String, String, Vec<String>, Vec<String>, String, Vec<DescribedTuple>, Vec<String>, Vec<DocTuple>, usize);

fn step_tuple(step: Step) -> StepTuple {
    (
        step.task,
        step.command,
        step.issues,
        step.misses,
        step.closest,
        step.described.into_iter().map(described_tuple).collect(),
        step.warnings,
        step.notes.into_iter().map(doc_tuple).collect(),
        step.tries,
    )
}

fn step_from(obj: &Bound<'_, PyAny>) -> PyResult<Step> {
    let described = obj.getattr("described")?.try_iter()?.map(|d| described_from(&d?)).collect::<PyResult<_>>()?;
    Ok(Step {
        task: obj.getattr("task")?.extract()?,
        command: obj.getattr("command")?.extract()?,
        issues: obj.getattr("issues")?.extract()?,
        misses: obj.getattr("misses")?.extract()?,
        closest: obj.getattr("closest")?.extract()?,
        described,
        warnings: obj.getattr("warnings")?.extract()?,
        notes: docs_from(&obj.getattr("notes")?)?,
        tries: obj.getattr("tries")?.extract()?,
    })
}

fn steps_from(steps: &Bound<'_, PyAny>) -> PyResult<Vec<Step>> {
    steps.try_iter()?.map(|s| step_from(&s?)).collect()
}

// prompt

#[pyfunction]
#[pyo3(signature = (docs, max_chars = prompt::MAX_CONTEXT_CHARS))]
fn format_context(docs: &Bound<'_, PyAny>, max_chars: usize) -> PyResult<String> {
    Ok(prompt::format_context(&docs_from(docs)?, max_chars))
}

#[pyfunction]
fn format_doc(doc: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(prompt::format_doc(&doc_from(doc)?))
}

#[pyfunction]
#[pyo3(signature = (task, docs = None))]
fn build_messages<'py>(py: Python<'py>, task: &str, docs: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyList>> {
    let docs = match docs {
        Some(docs) => docs_from(docs)?,
        None => Vec::new(),
    };
    messages_list(py, &prompt::build_messages(task, &docs))
}

#[pyfunction]
fn build_split_messages<'py>(py: Python<'py>, request: &str) -> PyResult<Bound<'py, PyList>> {
    messages_list(py, &prompt::build_split_messages(request))
}

#[pyfunction]
fn format_answer(command: &str) -> String {
    prompt::format_answer(command)
}

#[pyfunction]
fn parse_answer(text: &str) -> String {
    prompt::parse_answer(text)
}

#[pyfunction]
fn format_tasks(tasks: &Bound<'_, PyAny>) -> PyResult<String> {
    let tasks: Vec<String> = tasks.try_iter()?.map(|t| t?.extract()).collect::<PyResult<_>>()?;
    Ok(prompt::format_tasks(&tasks))
}

#[pyfunction]
#[pyo3(signature = (text, limit = prompt::MAX_TASKS))]
fn parse_tasks(text: &str, limit: usize) -> Vec<String> {
    prompt::parse_tasks(text, limit)
}

// split

#[pyfunction]
fn needs_split(question: &str) -> bool {
    split::needs_split(question)
}

#[pyfunction]
fn tasks_for(py: Python<'_>, question: &str, chat: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let mut failure: Option<PyErr> = None;
    let mut call = |messages: &[Message]| -> Option<String> {
        let result = messages_list(py, messages)
            .and_then(|list| chat.call1((list,)))
            .and_then(|reply| reply.extract::<Option<String>>());
        match result {
            Ok(reply) => reply,
            Err(error) => {
                failure.get_or_insert(error);
                None
            }
        }
    };
    let tasks = split::tasks_for(question, &mut call);
    match failure {
        Some(error) => Err(error),
        None => Ok(tasks),
    }
}

// constraints

#[pyfunction]
#[pyo3(signature = (task, command, request = ""))]
fn constraints_check(task: &str, command: &str, request: &str) -> Vec<(&'static str, String)> {
    constraints::check(task, command, request).into_iter().map(|m| (m.kind, m.detail)).collect()
}

#[pyfunction]
#[pyo3(signature = (command, task = ""))]
fn repair(command: &str, task: &str) -> String {
    constraints::repair(command, task)
}

#[pyfunction]
fn clarification(question: &str) -> Option<String> {
    lxask_core::clarify::clarification(question)
}

#[pyfunction]
fn about_itself(question: &str) -> bool {
    lxask_core::about::about_itself(question)
}

#[pyfunction]
fn forbidden(text: &str) -> Vec<String> {
    constraints::forbidden(text)
}

#[pyfunction]
#[pyo3(signature = (question, catalog = None))]
fn specialist(question: &str, catalog: Option<PyRef<'_, Catalog>>) -> bool {
    lxask_core::route::specialist(question, catalog.as_ref().map(|c| &c.inner))
}

#[pyfunction]
fn programs_run(command: &str) -> Vec<String> {
    validate::programs_run(command)
}

fn num_object<'py>(py: Python<'py>, n: Num) -> PyResult<Bound<'py, PyAny>> {
    Ok(match n {
        Num::Int(i) => i.into_pyobject(py)?.into_any(),
        Num::Float(f) => f.into_pyobject(py)?.into_any(),
    })
}

#[pyfunction]
fn requirements<'py>(py: Python<'py>, task: &str) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    for (kind, written, wanted) in constraints::requirements(task) {
        let wanted: Bound<'py, PyAny> = match wanted {
            Wanted::Numbers(numbers) => {
                let set = PySet::empty(py)?;
                for n in numbers.0 {
                    set.add(num_object(py, n)?)?;
                }
                set.into_any()
            }
            Wanted::Text(text) => text.into_pyobject(py)?.into_any(),
            Wanted::Nothing => py.None().into_bound(py),
        };
        out.append(PyTuple::new(py, [kind.into_pyobject(py)?.into_any(), written.into_pyobject(py)?.into_any(), wanted])?)?;
    }
    Ok(out)
}

// safety, loops

#[pyfunction]
fn find_warnings(command: &str) -> Vec<String> {
    safety::find_warnings(command)
}

#[pyfunction]
fn looks_looped(text: &str) -> bool {
    looped::looks_looped(text)
}

#[pyfunction]
fn strip_thinking(text: &str) -> String {
    looped::strip_thinking(text)
}

// validate

/// What the documentation knows: program names, and each program's options.
#[pyclass(module = "lxask._core")]
#[derive(Clone)]
struct Catalog {
    inner: validate::Catalog,
}

#[pymethods]
impl Catalog {
    #[new]
    fn new() -> Catalog {
        Catalog { inner: validate::Catalog::default() }
    }

    #[getter]
    fn programs(&self) -> HashSet<String> {
        self.inner.programs.clone()
    }

    #[getter]
    fn with_subcommands(&self) -> HashSet<String> {
        self.inner.with_subcommands.clone()
    }

    #[getter]
    fn options(&self) -> HashMap<String, HashMap<String, bool>> {
        self.inner.options.clone()
    }

    #[getter]
    fn texts(&self) -> HashMap<String, HashMap<String, String>> {
        self.inner.texts.clone()
    }

    #[getter]
    fn summaries(&self) -> HashMap<String, String> {
        self.inner.summaries.clone()
    }

    #[getter]
    fn subcommands(&self) -> HashMap<String, HashSet<String>> {
        self.inner.subcommands.clone()
    }

    fn add_subcommand(&mut self, program: &str, verb: &str) {
        self.inner.add_subcommand(program, verb)
    }

    fn add_program(&mut self, name: &str) {
        self.inner.add_program(name)
    }

    #[pyo3(signature = (page, title, text = ""))]
    fn add_option_title(&mut self, page: &str, title: &str, text: &str) {
        self.inner.add_option_title(page, title, text)
    }

    fn add_example(&mut self, command: &str) {
        self.inner.add_example(command)
    }

    fn add_summary(&mut self, page: &str, summary: &str) {
        self.inner.summaries.insert(page.to_string(), summary.to_string());
    }

    fn has_program(&self, name: &str) -> bool {
        self.inner.programs.contains(name)
    }

    fn subcommand(&self, name: &str, arguments: Vec<String>) -> Option<String> {
        self.inner.subcommand(name, &arguments).map(String::from)
    }

    fn pages_for(&self, name: &str, arguments: Vec<String>) -> Vec<String> {
        self.inner.pages_for(name, &arguments)
    }

    fn options_for(&self, name: &str, arguments: Vec<String>) -> Option<HashMap<String, bool>> {
        self.inner.options_for(name, &arguments)
    }

    fn text_for(&self, name: &str, arguments: Vec<String>, flag: &str) -> String {
        self.inner.text_for(name, &arguments, flag)
    }

    fn summary_for(&self, name: &str, arguments: Vec<String>) -> String {
        self.inner.summary_for(name, &arguments)
    }

    fn __len__(&self) -> usize {
        self.inner.programs.len()
    }

    fn __repr__(&self) -> String {
        format!("Catalog({} programs, {} option pages)", self.inner.programs.len(), self.inner.options.len())
    }
}

#[pyfunction]
fn parse_option_title(title: &str) -> Vec<(String, bool)> {
    validate::parse_option_title(title)
}

#[pyfunction]
fn simple_commands(command: &str) -> PyResult<Vec<Vec<String>>> {
    validate::simple_commands(command).map_err(PyValueError::new_err)
}

type UseTuple = (String, &'static str, Vec<String>, Vec<(String, Option<Vec<String>>)>);

#[pyfunction]
fn uses(command: &str, catalog: &Catalog) -> PyResult<Vec<UseTuple>> {
    let found = validate::uses(command, &catalog.inner).map_err(PyValueError::new_err)?;
    Ok(found.into_iter().map(|u| (u.program, u.status, u.arguments, u.options)).collect())
}

#[pyfunction]
#[pyo3(signature = (command, catalog, syntax = false))]
fn validate_check(command: &str, catalog: &Catalog, syntax: bool) -> Vec<(String, &'static str, String)> {
    validate::check(command, &catalog.inner, syntax).into_iter().map(|i| (i.program, i.kind, i.detail)).collect()
}

#[pyfunction]
fn bash_syntax_error(command: &str) -> Option<String> {
    validate::bash_syntax_error(command)
}

#[pyfunction]
#[pyo3(signature = (programs, option_titles, examples = None, summaries = None))]
fn build_catalog(
    programs: &Bound<'_, PyAny>,
    option_titles: &Bound<'_, PyAny>,
    examples: Option<&Bound<'_, PyAny>>,
    summaries: Option<&Bound<'_, PyAny>>,
) -> PyResult<Catalog> {
    let programs: Vec<String> = programs.try_iter()?.map(|p| p?.extract()).collect::<PyResult<_>>()?;
    let mut titles: Vec<(String, String, String)> = Vec::new();
    for entry in option_titles.try_iter()? {
        let entry: Vec<String> = entry?.extract()?;
        match entry.as_slice() {
            [page, title] => titles.push((page.clone(), title.clone(), String::new())),
            [page, title, text, ..] => titles.push((page.clone(), title.clone(), text.clone())),
            _ => return Err(PyValueError::new_err("an option title is (page, title[, description])")),
        }
    }
    let examples: Vec<String> = match examples {
        Some(examples) => examples.try_iter()?.map(|e| e?.extract()).collect::<PyResult<_>>()?,
        None => Vec::new(),
    };
    let summaries: Vec<(String, String)> = match summaries {
        Some(summaries) if !summaries.is_none() => {
            let dict: HashMap<String, String> = summaries.extract()?;
            dict.into_iter().collect()
        }
        _ => Vec::new(),
    };
    Ok(Catalog {
        inner: validate::build_catalog(
            programs.iter().map(String::as_str),
            titles.iter().map(|(p, t, x)| (p.as_str(), t.as_str(), x.as_str())),
            examples.iter().map(String::as_str),
            summaries,
        ),
    })
}

#[pyfunction]
fn save_catalog(db_path: PathBuf, catalog: &Catalog) -> PyResult<()> {
    validate::save_catalog(&db_path, &catalog.inner).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

#[pyfunction]
fn load_catalog(db_path: PathBuf) -> Option<Catalog> {
    validate::load_catalog(&db_path).map(|inner| Catalog { inner })
}

// explain

#[pyfunction]
#[pyo3(signature = (text, limit = explain::MAX_CHARS))]
fn brief(text: &str, limit: usize) -> String {
    explain::brief(text, limit)
}

#[pyfunction]
fn explain_command(command: &str, catalog: &Catalog) -> Vec<DescribedTuple> {
    explain::explain(command, &catalog.inner).into_iter().map(described_tuple).collect()
}

#[pyfunction]
fn render_described(described: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let described: Vec<Described> = described.try_iter()?.map(|d| described_from(&d?)).collect::<PyResult<_>>()?;
    Ok(explain::render(&described))
}

// retrieve

#[pyfunction]
#[pyo3(signature = (question, synonyms = true))]
fn fts_query(question: &str, synonyms: bool) -> String {
    retrieve::fts_query(question, synonyms)
}

#[pyfunction]
fn program(cmd: &str) -> String {
    retrieve::program(cmd).to_string()
}

/// Search over the docs index; with `embed` (texts -> unit vectors) it is semantic.
#[pyclass(module = "lxask._core", unsendable)]
struct Retriever {
    inner: Option<retrieve::Retriever>,
}

impl Retriever {
    fn get(&mut self) -> PyResult<&mut retrieve::Retriever> {
        self.inner.as_mut().ok_or_else(|| PyValueError::new_err("the retriever is closed"))
    }
}

#[pymethods]
impl Retriever {
    #[new]
    #[pyo3(signature = (db_path, embed = None, synonyms = true))]
    fn new(db_path: PathBuf, embed: Option<Py<PyAny>>, synonyms: bool) -> PyResult<Retriever> {
        let embed: Option<retrieve::Embed> = embed.map(|embed| {
            Box::new(move |texts: &[String]| -> Result<Vec<Vec<f32>>, Error> {
                Python::attach(|py| {
                    let mut vectors = embed.bind(py).call1((texts.to_vec(),))?;
                    if vectors.hasattr("tolist")? {
                        vectors = vectors.call_method0("tolist")?; // a numpy array
                    }
                    Ok(vectors.extract::<Vec<Vec<f32>>>()?)
                })
            }) as retrieve::Embed
        });
        let inner = retrieve::Retriever::new(&db_path, embed, synonyms)
            .map_err(|e| PyRuntimeError::new_err(format!("cannot open the docs index {}: {e}", db_path.display())))?;
        Ok(Retriever { inner: Some(inner) })
    }

    #[getter]
    fn semantic(&mut self) -> PyResult<bool> {
        Ok(self.get()?.semantic())
    }

    #[getter]
    fn read_only(&mut self) -> PyResult<bool> {
        Ok(self.get()?.read_only())
    }

    #[pyo3(signature = (question, n = 4))]
    fn commands(&mut self, question: &str, n: usize) -> PyResult<Vec<String>> {
        self.get()?.commands(question, n).map_err(to_py_err)
    }

    #[pyo3(signature = (question, n_cmds = retrieve::N_COMMANDS, top = retrieve::TOP_EXAMPLES, per_cmd = retrieve::OTHER_EXAMPLES))]
    fn search(&mut self, question: &str, n_cmds: usize, top: usize, per_cmd: usize) -> PyResult<Vec<DocTuple>> {
        let docs = self.get()?.search(question, n_cmds, top, per_cmd).map_err(to_py_err)?;
        Ok(docs.into_iter().map(doc_tuple).collect())
    }

    fn close(&mut self) {
        self.inner = None;
    }
}

// answer

fn search_of<'a, 'py>(search: &'a Bound<'py, PyAny>) -> impl FnMut(&str) -> Result<Vec<Doc>, Error> + 'a
where
    'py: 'a,
{
    move |task: &str| Ok(docs_from(&search.call1((task,))?)?)
}

#[pyfunction]
#[pyo3(name = "answer", signature = (question, chat, search, catalog, resample = None))]
fn answer_steps(
    py: Python<'_>,
    question: &str,
    chat: &Bound<'_, PyAny>,
    search: &Bound<'_, PyAny>,
    catalog: Option<PyRef<'_, Catalog>>,
    resample: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<StepTuple>> {
    let mut chat = chat_of(py, chat);
    let mut search = search_of(search);
    let mut resample_fn = resample.filter(|r| !r.is_none()).map(|r| resample_of(py, r));
    let catalog = catalog.as_ref().map(|c| &c.inner);
    let steps = answer::answer(
        question,
        &mut chat,
        &mut search,
        catalog,
        resample_fn.as_mut().map(|f| f as &mut answer::Resample),
    )
    .map_err(to_py_err)?;
    Ok(steps.into_iter().map(step_tuple).collect())
}

#[pyfunction]
#[pyo3(name = "split", signature = (question, chat, resample = None))]
fn split_request(py: Python<'_>, question: &str, chat: &Bound<'_, PyAny>, resample: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<String>> {
    let mut chat = chat_of(py, chat);
    let mut resample_fn = resample.filter(|r| !r.is_none()).map(|r| resample_of(py, r));
    answer::split(question, &mut chat, resample_fn.as_mut().map(|f| f as &mut answer::Resample)).map_err(to_py_err)
}

#[pyfunction]
#[pyo3(signature = (steps, explanations = true, question = None))]
fn render(steps: &Bound<'_, PyAny>, explanations: bool, question: Option<&str>) -> PyResult<String> {
    Ok(answer::render(&steps_from(steps)?, explanations, question))
}

#[pyfunction]
fn render_command(command: &str) -> Vec<String> {
    answer::render_command(command)
}

#[pyfunction]
fn unhandled(question: &str, steps: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    Ok(answer::unhandled(question, &steps_from(steps)?))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("ANSWER_SYSTEM", prompt::ANSWER_SYSTEM)?;
    m.add("SPLIT_SYSTEM", prompt::SPLIT_SYSTEM)?;
    m.add("MAX_CONTEXT_CHARS", prompt::MAX_CONTEXT_CHARS)?;
    m.add("MAX_TASKS", prompt::MAX_TASKS)?;
    m.add("SINGLE_TASK_WORDS", split::SINGLE_TASK_WORDS)?;
    m.add("RETRIES", answer::RETRIES)?;
    m.add("MAX_CHARS", explain::MAX_CHARS)?;
    m.add("MIN_OPTIONS", validate::MIN_OPTIONS)?;
    m.add("MIN_SUBCOMMAND_PAGES", validate::MIN_SUBCOMMAND_PAGES)?;
    m.add("BUILTINS", PyFrozenSet::new(py, validate::BUILTINS)?)?;
    m.add("NOT_LINUX_PROGRAMS", PyFrozenSet::new(py, validate::NOT_LINUX_PROGRAMS)?)?;
    m.add("VERB_PROGRAMS", PyFrozenSet::new(py, validate::VERB_PROGRAMS)?)?;
    m.add("CATALOG_SCHEMA", PyTuple::new(py, validate::CATALOG_SCHEMA)?)?;
    m.add("STOPWORDS", PyFrozenSet::new(py, retrieve::STOPWORDS)?)?;
    let synonyms = PyDict::new(py);
    for (word, alternatives) in retrieve::SYNONYMS {
        synonyms.set_item(word, PyTuple::new(py, *alternatives)?)?;
    }
    m.add("SYNONYMS", synonyms)?;
    m.add("QUERY_PREFIX", retrieve::QUERY_PREFIX)?;
    m.add("PRIOR_WEIGHT", retrieve::PRIOR_WEIGHT)?;
    m.add("UNNAMED_PENALTY", retrieve::UNNAMED_PENALTY)?;
    m.add("N_COMMANDS", retrieve::N_COMMANDS)?;
    m.add("TOP_EXAMPLES", retrieve::TOP_EXAMPLES)?;
    m.add("OTHER_EXAMPLES", retrieve::OTHER_EXAMPLES)?;

    m.add_function(wrap_pyfunction!(format_context, m)?)?;
    m.add_function(wrap_pyfunction!(format_doc, m)?)?;
    m.add_function(wrap_pyfunction!(build_messages, m)?)?;
    m.add_function(wrap_pyfunction!(build_split_messages, m)?)?;
    m.add_function(wrap_pyfunction!(format_answer, m)?)?;
    m.add_function(wrap_pyfunction!(parse_answer, m)?)?;
    m.add_function(wrap_pyfunction!(format_tasks, m)?)?;
    m.add_function(wrap_pyfunction!(parse_tasks, m)?)?;
    m.add_function(wrap_pyfunction!(needs_split, m)?)?;
    m.add_function(wrap_pyfunction!(tasks_for, m)?)?;
    m.add_function(wrap_pyfunction!(constraints_check, m)?)?;
    m.add_function(wrap_pyfunction!(repair, m)?)?;
    m.add_function(wrap_pyfunction!(forbidden, m)?)?;
    m.add_function(wrap_pyfunction!(specialist, m)?)?;
    m.add_function(wrap_pyfunction!(clarification, m)?)?;
    m.add_function(wrap_pyfunction!(about_itself, m)?)?;
    m.add_function(wrap_pyfunction!(programs_run, m)?)?;
    m.add_function(wrap_pyfunction!(requirements, m)?)?;
    m.add_function(wrap_pyfunction!(find_warnings, m)?)?;
    m.add_function(wrap_pyfunction!(looks_looped, m)?)?;
    m.add_function(wrap_pyfunction!(strip_thinking, m)?)?;
    m.add_class::<Catalog>()?;
    m.add_function(wrap_pyfunction!(parse_option_title, m)?)?;
    m.add_function(wrap_pyfunction!(simple_commands, m)?)?;
    m.add_function(wrap_pyfunction!(uses, m)?)?;
    m.add_function(wrap_pyfunction!(validate_check, m)?)?;
    m.add_function(wrap_pyfunction!(bash_syntax_error, m)?)?;
    m.add_function(wrap_pyfunction!(build_catalog, m)?)?;
    m.add_function(wrap_pyfunction!(save_catalog, m)?)?;
    m.add_function(wrap_pyfunction!(load_catalog, m)?)?;
    m.add_function(wrap_pyfunction!(brief, m)?)?;
    m.add_function(wrap_pyfunction!(explain_command, m)?)?;
    m.add_function(wrap_pyfunction!(render_described, m)?)?;
    m.add_function(wrap_pyfunction!(fts_query, m)?)?;
    m.add_function(wrap_pyfunction!(program, m)?)?;
    m.add_class::<Retriever>()?;
    m.add_function(wrap_pyfunction!(answer_steps, m)?)?;
    m.add_function(wrap_pyfunction!(split_request, m)?)?;
    m.add_function(wrap_pyfunction!(render, m)?)?;
    m.add_function(wrap_pyfunction!(render_command, m)?)?;
    m.add_function(wrap_pyfunction!(unhandled, m)?)?;
    Ok(())
}
