//! Python's `shlex.shlex(posix=True)` with `whitespace_split=True` and no comment
//! characters, optionally with `punctuation_chars=True`: the two lexers lxask uses
//! (`shlex.split` and the validator's command splitter). Ported state for state so
//! that commands split into the same words.

const WHITESPACE: &str = " \t\r\n";
const PUNCTUATION: &str = "();<>|&";

#[derive(Clone, Copy, PartialEq)]
enum State {
    Space,
    Word,
    Punct,
    Quote(char),
    Escape,
    Done,
}

pub struct Lexer {
    chars: Vec<char>,
    pos: usize,
    state: State,
    punctuation: bool,
    pushback: Vec<char>,
}

impl Lexer {
    pub fn new(text: &str, punctuation: bool) -> Lexer {
        Lexer { chars: text.chars().collect(), pos: 0, state: State::Space, punctuation, pushback: Vec::new() }
    }

    fn next_char(&mut self) -> Option<char> {
        if self.punctuation {
            if let Some(c) = self.pushback.pop() {
                return Some(c);
            }
        }
        let c = self.chars.get(self.pos).copied();
        if c.is_some() {
            self.pos += 1;
        }
        c
    }

    fn is_punct(&self, c: char) -> bool {
        self.punctuation && PUNCTUATION.contains(c)
    }

    /// The next word, None at the end; Err with Python's message on a bad line.
    pub fn read_token(&mut self) -> Result<Option<String>, &'static str> {
        let mut quoted = false;
        let mut escaped_state = State::Space;
        let mut token = String::new();
        loop {
            let next = self.next_char();
            match self.state {
                State::Done => {
                    token.clear();
                    break;
                }
                State::Space => match next {
                    None => {
                        self.state = State::Done;
                        break;
                    }
                    Some(c) if WHITESPACE.contains(c) => {
                        if !token.is_empty() || quoted {
                            break;
                        }
                    }
                    Some('\\') => {
                        escaped_state = State::Word;
                        self.state = State::Escape;
                    }
                    Some(c) if self.is_punct(c) => {
                        token.push(c);
                        self.state = State::Punct;
                    }
                    Some(c @ ('\'' | '"')) => self.state = State::Quote(c),
                    Some(c) => {
                        token.push(c);
                        self.state = State::Word;
                    }
                },
                State::Quote(quote) => {
                    quoted = true;
                    match next {
                        None => return Err("No closing quotation"),
                        Some(c) if c == quote => self.state = State::Word,
                        Some('\\') if quote == '"' => {
                            escaped_state = self.state;
                            self.state = State::Escape;
                        }
                        Some(c) => token.push(c),
                    }
                }
                State::Escape => {
                    let Some(c) = next else { return Err("No escaped character") };
                    // In quotes only the quote itself or the escape character may be escaped.
                    if let State::Quote(quote) = escaped_state {
                        if c != '\\' && c != quote {
                            token.push('\\');
                        }
                    }
                    token.push(c);
                    self.state = escaped_state;
                }
                State::Word | State::Punct => match next {
                    None => {
                        self.state = State::Done;
                        break;
                    }
                    Some(c) if WHITESPACE.contains(c) => {
                        self.state = State::Space;
                        if !token.is_empty() || quoted {
                            break;
                        }
                    }
                    Some(c) if self.state == State::Punct => {
                        if self.is_punct(c) {
                            token.push(c);
                        } else {
                            self.pushback.push(c); // not whitespace: that was handled above
                            self.state = State::Space;
                            break;
                        }
                    }
                    Some(c @ ('\'' | '"')) => self.state = State::Quote(c),
                    Some('\\') => {
                        escaped_state = State::Word;
                        self.state = State::Escape;
                    }
                    Some(c) if !self.is_punct(c) => token.push(c),
                    Some(c) => {
                        self.pushback.push(c);
                        self.state = State::Space;
                        if !token.is_empty() || quoted {
                            break;
                        }
                    }
                },
            }
        }
        if !quoted && token.is_empty() {
            return Ok(None);
        }
        Ok(Some(token))
    }

    /// Every word, as `list(lexer)`.
    pub fn tokens(mut self) -> Result<Vec<String>, &'static str> {
        let mut tokens = Vec::new();
        while let Some(token) = self.read_token()? {
            tokens.push(token);
        }
        Ok(tokens)
    }
}

/// `shlex.split(text)`.
pub fn split(text: &str) -> Result<Vec<String>, &'static str> {
    Lexer::new(text, false).tokens()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn punct(text: &str) -> Vec<String> {
        Lexer::new(text, true).tokens().unwrap()
    }

    #[test]
    fn split_like_python() {
        assert_eq!(split("a 'b c' \"d\\\"e\" f\\ g").unwrap(), vec!["a", "b c", "d\"e", "f g"]);
        assert_eq!(split("x '' y").unwrap(), vec!["x", "", "y"]);
        assert_eq!(split("\"a\\nb\"").unwrap(), vec!["a\\nb"]);
        assert_eq!(split("a|b").unwrap(), vec!["a|b"]);
        assert_eq!(split("'unterminated"), Err("No closing quotation"));
        assert_eq!(split("trailing\\"), Err("No escaped character"));
    }

    #[test]
    fn punctuation_like_python() {
        // shlex.shlex(..., posix=True, punctuation_chars=True), whitespace_split=True
        assert_eq!(punct("ls -l|grep x"), vec!["ls", "-l", "|", "grep", "x"]);
        assert_eq!(punct("cmd 2>&1 >out"), vec!["cmd", "2", ">&", "1", ">", "out"]);
        assert_eq!(punct("echo $((1+2))"), vec!["echo", "$", "((", "1+2", "))"]);
        assert_eq!(punct("a && b || c;d"), vec!["a", "&&", "b", "||", "c", ";", "d"]);
        assert_eq!(punct("x='a b'c"), vec!["x=a bc"]);
        assert_eq!(punct("a(\"b\")"), vec!["a", "(", "b", ")"]);
        assert_eq!(punct("f() { :; }"), vec!["f", "()", "{", ":", ";", "}"]);
    }
}
