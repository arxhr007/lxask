# Contributing

Thanks for helping. The most useful contribution is a wrong answer.

## Report a wrong answer

```sh
lxask "what you asked"
lxask --bad "what was wrong, or the right command"
lxask --share            # prints the report and a link to a prefilled issue
```

Open the link, remove anything private, and submit. Wrong answers become regression
cases (`evalsets/regressions.jsonl`) that every later model must pass. You can also
open a "Bad answer" issue by hand.

## Report a bug or suggest something

Open a "Something else" issue: what you ran, what happened, what you expected, and
`lxask --version`. For security problems, see [SECURITY.md](SECURITY.md).

## Change the code

- Setup, building and tests: [docs/development.md](docs/development.md).
- The rules (checks, repairs, routing) are in `rust/crates/lxask-core`, and the
  Python training and evaluation scripts call the same code through `lxask._core`.
  Rust tests: `cargo test --manifest-path rust/Cargo.toml -p lxask-core`; Python
  tests: `pytest` (no model needed).
- A change to a rule should not change answers it is not about. Run the evaluation
  before and after (`scripts/eval.py`, see [docs/development.md](docs/development.md#measuring-it))
  and say in the pull request what moved.
- Keep commits small and their messages about why.

## Training data

The training data written by language models is not in this repository (see
[NOTICE](NOTICE) and [docs/development.md](docs/development.md#training-data)).
`scripts/generate_data.py` makes it again with your own API key; read your
model provider's terms on using its output for training before you do.

## Licence

By contributing you agree that your contribution is under the MIT License of this
repository ([LICENSE](LICENSE)).
