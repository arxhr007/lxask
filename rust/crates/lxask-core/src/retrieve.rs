//! Search over the docs index.
//!
//! Search is command-first: pick the few commands the question is about, weighted by
//! how common each command is, then pull the best-matching examples of those.
//!
//! With an embedding model the match is semantic ("make a script executable" finds
//! chmod). Without one it falls back to BM25 over the same index, which needs the
//! question to share words with the documentation.

use std::collections::HashMap;
use std::path::Path;

use rusqlite::Connection;

use crate::prompt::Doc;
use crate::py::{self, LazyRe};
use crate::validate::open_read_only;
use crate::{re, Error};

pub const STOPWORDS: &[&str] = &[
    "a", "am", "an", "and", "any", "are", "as", "at", "be", "by", "can", "command", "commands", "could", "do", "does",
    "for", "from", "get", "give", "have", "how", "i", "if", "in", "into", "is", "it", "its", "linux", "me", "my",
    "need", "of", "on", "or", "please", "should", "show", "so", "tell", "than", "that", "the", "their", "them",
    "then", "there", "this", "to", "up", "use", "using", "want", "way", "we", "what", "when", "where", "which",
    "will", "with", "would", "you", "your",
];

/// Everyday wording -> the wording the documentation uses. Lexical search cannot bridge
/// these on its own.
pub const SYNONYMS: &[(&str, &[&str])] = &[
    ("big", &["large"]),
    ("bigger", &["larger", "greater"]),
    ("biggest", &["largest"]),
    ("boot", &["startup", "enable"]),
    ("delete", &["remove"]),
    ("dir", &["directory"]),
    ("erase", &["remove"]),
    ("folder", &["directory"]),
    ("ram", &["memory"]),
    ("rename", &["move"]),
    ("uncompress", &["decompress", "extract"]),
    ("untar", &["tar", "extract"]),
    ("unzip", &["extract"]),
    ("url", &["download"]),
    ("view", &["display", "print"]),
    ("zip", &["compress", "archive"]),
];

/// bge models are trained with this instruction on the query side only.
pub const QUERY_PREFIX: &str = "Represent this sentence for searching relevant passages: ";

/// Cosine similarity gained per unit of command prior (priors run from 1 to 2.5).
/// Tuned on evalsets/everyday.jsonl: 0.10-0.15 is the plateau, 0.20 already lets common
/// commands crowd out the right answer.
pub const PRIOR_WEIGHT: f64 = 0.15;

/// Similarity taken from tools with no evidence of being common (not on a stock install,
/// never seen in NL2Bash) unless the question names them. tldr documents thousands of
/// these; without this, "list open ports" retrieves `openstack port list`.
pub const UNNAMED_PENALTY: f64 = 0.2;

/// How many notes reach the prompt. Which commands are found decides recall, so four
/// commands are kept; extra examples of each add little and cost prompt length, which a
/// multi-step answer pays once per step.
pub const N_COMMANDS: usize = 4;
pub const TOP_EXAMPLES: usize = 4;
pub const OTHER_EXAMPLES: usize = 1;

// bm25() column weights: cmd first. A question that names a command should land on it.
// The prior can lift a page by at most a small factor, so it is applied to a shortlist;
// reading it for every matching page would touch the whole table.
const PAGES: &str = "
SELECT p.cmd, t.score * p.prior AS score
FROM (
    SELECT rowid, bm25(pages, 5.0, 1.0) AS score
    FROM pages WHERE pages MATCH ? ORDER BY score LIMIT 100
) AS t JOIN pages AS p ON p.rowid = t.rowid
ORDER BY score LIMIT ?
";
// The query names the command, so the full-text index narrows to its entries first.
// That phrase also matches longer names (`git commit-tree`), hence `cmd = ?`.
const ROWS: &str = "
SELECT cmd, title, body, source, bm25(docs, 0.0, 2.0, 1.0) AS score
FROM docs WHERE docs MATCH ? AND cmd = ? AND source = ? ORDER BY score LIMIT ?
";

/// Texts in, one unit-length vector per text out.
pub type Embed = Box<dyn FnMut(&[String]) -> Result<Vec<Vec<f32>>, Error>>;

fn expand(term: &str) -> &'static [&'static str] {
    let lookup = |t: &str| SYNONYMS.iter().find(|(word, _)| *word == t).map(|(_, s)| *s);
    lookup(term).or_else(|| lookup(term.strip_suffix('s').unwrap_or(term))).unwrap_or(&[])
}

static TERM: LazyRe = re!(r"[a-z0-9]+");
static WORD: LazyRe = re!(r"[a-z0-9][a-z0-9._+-]*");

/// Turn a question into an FTS5 query that ORs its content words.
pub fn fts_query(question: &str, synonyms: bool) -> String {
    let lowered = question.to_lowercase();
    let mut terms: Vec<String> = Vec::new();
    for caps in TERM.finditer(&lowered) {
        let term = py::group_or_empty(&caps, 0);
        if py::char_len(term) < 2 || STOPWORDS.contains(&term) {
            continue;
        }
        let mut candidates = vec![term];
        if synonyms {
            candidates.extend(expand(term).iter().copied());
        }
        for candidate in candidates {
            if !terms.iter().any(|t| t == candidate) {
                terms.push(candidate.to_string());
            }
        }
    }
    terms.iter().map(|t| format!("\"{t}\"")).collect::<Vec<_>>().join(" OR ")
}

/// `git commit` -> `git`: the executable behind a (sub)command page.
pub fn program(cmd: &str) -> &str {
    if py::strip(cmd).is_empty() { cmd } else { py::split_ws(cmd)[0] }
}

/// The embedded entries, aligned row for row with their metadata.
struct Vectors {
    rowids: Vec<i64>,
    dim: usize,
    matrix: Vec<f32>,
    cmds: Vec<String>,
    bonus: Vec<f32>,
    unnamed: Vec<f32>,
    /// rows of tools with no evidence of being common, by program name
    uncommon: HashMap<String, Vec<usize>>,
    /// cmd -> positions of its examples, or of its summary when it has no examples
    by_cmd: HashMap<String, Vec<usize>>,
}

impl Vectors {
    fn load(con: &Connection) -> Option<Vectors> {
        let (rowids, vectors, dim): (Vec<u8>, Vec<u8>, i64) = con
            .query_row("SELECT rowids, vectors, dim FROM embeddings LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .ok()?; // index built without embeddings
        let rowids: Vec<i64> = rowids.chunks_exact(4).map(|b| i32::from_le_bytes([b[0], b[1], b[2], b[3]]) as i64).collect();
        let matrix: Vec<f32> = vectors.chunks_exact(2).map(|b| half::f16::from_le_bytes([b[0], b[1]]).to_f32()).collect();
        let dim = dim as usize;

        let mut entries: HashMap<i64, (String, String)> = HashMap::new();
        let mut statement = con.prepare("SELECT rowid, cmd, source FROM docs WHERE source != 'man'").ok()?;
        for row in statement.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?))).ok()? {
            let (rowid, cmd, source) = row.ok()?;
            entries.insert(rowid, (cmd, source));
        }
        let mut priors: HashMap<String, f64> = HashMap::new();
        let mut statement = con.prepare("SELECT cmd, prior FROM pages").ok()?;
        for row in statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, f64>(1)?))).ok()? {
            let (cmd, prior) = row.ok()?;
            priors.insert(cmd, prior);
        }

        let cmds: Vec<String> = rowids.iter().map(|rowid| entries[rowid].0.clone()).collect();
        // numpy: float32 priors, and float32 arithmetic with the weights
        let prior: Vec<f32> = cmds.iter().map(|cmd| priors.get(cmd).copied().unwrap_or(1.0) as f32).collect();
        let bonus = prior.iter().map(|p| PRIOR_WEIGHT as f32 * (p - 1.0)).collect();
        let unnamed = prior.iter().map(|&p| if p <= 1.0 { UNNAMED_PENALTY as f32 } else { 0.0 }).collect();
        let mut uncommon: HashMap<String, Vec<usize>> = HashMap::new();
        for (i, cmd) in cmds.iter().enumerate() {
            if prior[i] <= 1.0 {
                uncommon.entry(program(cmd).to_lowercase()).or_default().push(i);
            }
        }
        let mut by_cmd: HashMap<String, Vec<usize>> = HashMap::new();
        let mut summaries: HashMap<String, Vec<usize>> = HashMap::new();
        for (i, rowid) in rowids.iter().enumerate() {
            let (cmd, source) = &entries[rowid];
            let target = if source == "tldr" { &mut by_cmd } else { &mut summaries };
            target.entry(cmd.clone()).or_default().push(i);
        }
        for (cmd, positions) in summaries {
            by_cmd.entry(cmd).or_insert(positions);
        }
        Some(Vectors { rowids, dim, matrix, cmds, bonus, unnamed, uncommon, by_cmd })
    }
}

pub struct Retriever {
    con: Connection,
    synonyms: bool,
    embed: Option<Embed>,
    vectors: Option<Vectors>,
}

impl Retriever {
    /// `embed` turns on semantic search, provided the index was built with embeddings.
    pub fn new(db_path: &Path, embed: Option<Embed>, synonyms: bool) -> rusqlite::Result<Retriever> {
        let con = open_read_only(db_path)?;
        // fail now, as sqlite3.connect would on a missing or unreadable file
        con.query_row("SELECT count(*) FROM sqlite_master", [], |_| Ok(()))?;
        let vectors = if embed.is_some() { Vectors::load(&con) } else { None };
        Ok(Retriever { con, synonyms, embed, vectors })
    }

    pub fn semantic(&self) -> bool {
        self.vectors.is_some()
    }

    pub fn read_only(&self) -> bool {
        self.con.is_readonly(rusqlite::MAIN_DB).unwrap_or(false)
    }

    /// The `n` commands whose documentation best matches the question.
    pub fn commands(&mut self, question: &str, n: usize) -> Result<Vec<String>, Error> {
        if self.vectors.is_some() {
            if py::strip(question).is_empty() {
                return Ok(Vec::new());
            }
            let similarities = self.similarities(question)?;
            return Ok(self.rank(&similarities, question, n));
        }
        let query = fts_query(question, self.synonyms);
        if query.is_empty() {
            return Ok(Vec::new());
        }
        let mut statement = self.con.prepare_cached(PAGES)?;
        let cmds = statement
            .query_map(rusqlite::params![query, n as i64], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(cmds)
    }

    /// Examples for the `n_cmds` best commands: `top` for the best one, `per_cmd` for each other.
    pub fn search(&mut self, question: &str, n_cmds: usize, top: usize, per_cmd: usize) -> Result<Vec<Doc>, Error> {
        if self.vectors.is_some() {
            if py::strip(question).is_empty() {
                return Ok(Vec::new());
            }
            let similarities = self.similarities(question)?;
            let cmds = self.rank(&similarities, question, n_cmds);
            let mut picks = Vec::new();
            for (rank, cmd) in cmds.iter().enumerate() {
                picks.extend(self.best(&similarities, cmd, if rank == 0 { top } else { per_cmd }));
            }
            return self.fetch(&picks, &similarities);
        }
        let query = fts_query(question, self.synonyms);
        let mut docs = Vec::new();
        for (rank, cmd) in self.commands(question, n_cmds)?.iter().enumerate() {
            let limit = if rank == 0 { top } else { per_cmd };
            let mut found = self.rows(&query, cmd, "tldr", limit)?;
            if found.is_empty() {
                found = self.rows(&query, cmd, "summary", 1)?;
            }
            docs.extend(found);
        }
        Ok(docs)
    }

    // semantic search

    fn similarities(&mut self, question: &str) -> Result<Vec<f32>, Error> {
        let embed = self.embed.as_mut().expect("semantic search without a model");
        let text = format!("{QUERY_PREFIX}{}", py::strip(question));
        let vector = embed(&[text])?.into_iter().next().unwrap_or_default();
        let vectors = self.vectors.as_ref().unwrap();
        if vector.len() != vectors.dim {
            return Err(format!("the model gives {} dimensions, the index has {}", vector.len(), vectors.dim).into());
        }
        Ok(vectors
            .matrix
            .chunks_exact(vectors.dim)
            .map(|row| row.iter().zip(&vector).map(|(a, b)| *a as f64 * *b as f64).sum::<f64>() as f32)
            .collect())
    }

    /// What uncommon tools lose for not being named in the question.
    fn penalty(&self, question: &str) -> Vec<f32> {
        let vectors = self.vectors.as_ref().unwrap();
        let lowered = question.to_lowercase();
        let mut penalty = vectors.unnamed.clone();
        for caps in WORD.finditer(&lowered) {
            let word = py::rstrip_chars(py::group_or_empty(&caps, 0), "._+-");
            if let Some(rows) = vectors.uncommon.get(word) {
                for &i in rows {
                    penalty[i] = 0.0;
                }
            }
        }
        penalty
    }

    fn rank(&self, similarities: &[f32], question: &str, n: usize) -> Vec<String> {
        let vectors = self.vectors.as_ref().unwrap();
        let penalty = self.penalty(question);
        let scores: Vec<f32> = (0..similarities.len()).map(|i| similarities[i] + vectors.bonus[i] - penalty[i]).collect();
        let mut order: Vec<usize> = (0..scores.len()).collect();
        order.sort_by(|&a, &b| scores[b].partial_cmp(&scores[a]).unwrap_or(std::cmp::Ordering::Equal));
        let mut cmds: Vec<String> = Vec::new();
        for i in order {
            if !cmds.contains(&vectors.cmds[i]) {
                cmds.push(vectors.cmds[i].clone());
                if cmds.len() == n {
                    break;
                }
            }
        }
        cmds
    }

    /// The command's closest examples, or its summary if it has no examples.
    fn best(&self, similarities: &[f32], cmd: &str, limit: usize) -> Vec<usize> {
        let mut positions = self.vectors.as_ref().unwrap().by_cmd.get(cmd).cloned().unwrap_or_default();
        positions.sort_by(|&a, &b| similarities[b].partial_cmp(&similarities[a]).unwrap_or(std::cmp::Ordering::Equal));
        positions.truncate(limit);
        positions
    }

    fn fetch(&self, picks: &[usize], similarities: &[f32]) -> Result<Vec<Doc>, Error> {
        if picks.is_empty() {
            return Ok(Vec::new());
        }
        let vectors = self.vectors.as_ref().unwrap();
        let rowids: Vec<i64> = picks.iter().map(|&i| vectors.rowids[i]).collect();
        let marks = vec!["?"; rowids.len()].join(", ");
        let sql = format!("SELECT rowid, cmd, title, body, source FROM docs WHERE rowid IN ({marks})");
        let mut statement = self.con.prepare(&sql)?;
        let mut found: HashMap<i64, (String, String, String, String)> = HashMap::new();
        let rows = statement.query_map(rusqlite::params_from_iter(rowids.iter()), |r| {
            Ok((r.get::<_, i64>(0)?, (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))
        })?;
        for row in rows {
            let (rowid, fields) = row?;
            found.insert(rowid, fields);
        }
        Ok(picks
            .iter()
            .zip(&rowids)
            .map(|(&i, rowid)| {
                let (cmd, title, body, source) = found[rowid].clone();
                Doc { cmd, title, body, source, score: -(similarities[i] as f64) }
            })
            .collect())
    }

    // keyword search

    fn rows(&self, query: &str, cmd: &str, source: &str, limit: usize) -> Result<Vec<Doc>, Error> {
        let name = cmd.replace('"', "\"\"");
        let scoped = format!("cmd : \"{name}\" AND ({query})");
        let mut statement = self.con.prepare_cached(ROWS)?;
        let docs = statement
            .query_map(rusqlite::params![scoped, cmd, source, limit as i64], |r| {
                Ok(Doc { cmd: r.get(0)?, title: r.get(1)?, body: r.get(2)?, source: r.get(3)?, score: r.get(4)? })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(docs)
    }
}
