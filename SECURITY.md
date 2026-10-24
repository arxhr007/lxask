# Security

## What lxask does and does not do

- lxask **never runs the commands it suggests**. It prints them; you decide whether to
  run them. Read every command before you do, especially ones marked `warning:` or
  `unverified:`.
- It needs no network after installing and sends nothing anywhere. `lxask --share`
  only prints a link; nothing leaves your machine unless you open it and submit the
  issue yourself.
- `lxask --serve` listens on a Unix socket in a directory only your user can open
  (`$XDG_RUNTIME_DIR/lxask/`) and answers only that user.
- The installer checks every downloaded file against the release's `SHA256SUMS`.

## Reporting a problem

If you find a security problem (for example a way for one user to talk to another
user's server, a file written where it should not be, or the installer trusting
something it should not), please report it privately through GitHub's
[security advisory form](https://github.com/arxhr007/lxask/security/advisories/new)
rather than a public issue. Say what you found, how to reproduce it, and the lxask
version (`lxask --version`). If the form is not available, open an issue that only
asks for a private way to report, without the details.

A dangerous command suggested by the model is a wrong answer rather than a security
hole in the program: report it with `lxask --bad` and `lxask --share`, or as a "Bad
answer" issue.
