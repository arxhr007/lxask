# Configuration

lxask needs no configuration. Everything below is for when you want something
other than the default.

## Files

All in `~/.local/share/lxask` (`$LXASK_HOME`, else `$XDG_DATA_HOME/lxask`):

| file | what |
|---|---|
| `model.gguf` | link to the model in `models/` (about 990 MB) |
| `restrict.gguf` | link to the model's restriction adapter in `models/` (37 MB) |
| `precision.gguf` | link to the model's precision adapter in `models/` (37 MB) |
| `embed.gguf` | the encoder used to search the documentation (37 MB) |
| `docs.sqlite` | the documentation index: tldr pages and man page summaries and options (73 MB) |
| `last.json` | the last answer, for `lxask --bad` |
| `feedback.jsonl` | your `--bad` reports |
| `settings` | your choices, as `key = value` lines (below) |
| `token` | the secret other machines send to `lxask --serve --listen` (made by it, readable by you only) |
| `LICENSE`, `NOTICE` | licences of the files above |
| `*.previous` | what the last upgrade replaced |

The program itself is `~/.local/bin/lxask`.

## Environment variables

| variable | default | what |
|---|---|---|
| `LXASK_HOME` | `~/.local/share/lxask` | where the files above are |
| `LXASK_MODEL` | `$LXASK_HOME/model.gguf` | the model |
| `LXASK_ADAPTER` | `$LXASK_HOME/restrict.gguf` | the restriction adapter; `off` answers without it |
| `LXASK_PRECISION` | `$LXASK_HOME/precision.gguf` | the precision adapter; `off` answers without it |
| `LXASK_EMBED_MODEL` | `$LXASK_HOME/embed.gguf` | the encoder; without it, search is by keywords |
| `LXASK_INDEX` | `$LXASK_HOME/docs.sqlite` | the documentation index |
| `LXASK_PACKAGE_MANAGERS` | found on the machine | package managers to answer for, comma-separated (`apt,dpkg` or `dnf,rpm`) |
| `LXASK_AS_ROOT` | found from the user | `1`: don't add `sudo`; `0`: add it to commands that need root |
| `LXASK_GPU_LAYERS` | `0` | model layers on the GPU; only builds with a GPU backend (the releases are CPU only) |
| `LXASK_TIMING` | unset | set: print how long each start-up stage took and, for each answer, how its time split between reading the prompt, writing the reply and retrieval (the process that answers prints it: with a server, set it for `lxask --serve`) |
| `LXASK_SERVE` | the `serve` setting | `auto`: keep lxask loaded between questions; `off`: load it for each |
| `LXASK_SERVER` | the `server` setting | `host:port` of another machine's `lxask --serve --listen`, to ask it instead of a model here |
| `LXASK_TOKEN` | the `token` setting, else `$LXASK_HOME/token` | the token for `--listen`, or for asking `LXASK_SERVER` |

With `LXASK_SERVER`, the server answers as for its own machine: whether to add `sudo`
and which package manager to use come from there (`LXASK_AS_ROOT` and
`LXASK_PACKAGE_MANAGERS` in the server's environment), not from the machine asking.

`--model FILE` answers without the adapters, since an adapter is made for one model.
The `LINU_*` names of lxask 0.8 and earlier still work; `LXASK_*` win.

## Settings

`~/.local/share/lxask/settings`, written by the installer:

```ini
# lxask settings, written by install.sh
serve = auto      # keep lxask loaded between questions; off: load it for each question
```

`serve = auto` starts lxask in the background on the first question and stops it after
30 minutes without one ([usage.md](usage.md#keep-the-model-loaded)). Running the
installer again keeps your choice; `sh install.sh --serve` or `--no-serve` changes it.

On a machine that asks another one's server
([usage.md](usage.md#one-model-for-several-machines-or-containers)), two more lines do
what `LXASK_SERVER` and `LXASK_TOKEN` do:

```ini
server = 192.168.1.10:7871
token = <the token from the server's ~/.local/share/lxask/token>
```

## Package managers

lxask looks at which package managers the machine has (apt/dpkg, dnf/yum/rpm,
pacman, zypper, apk). If an answer uses another distribution's (`yum` on Ubuntu),
that counts as a mistake and is retried. Listing and searching packages are rewritten
for this machine: on Arch, `apt search x` becomes `pacman -Ss x`. A request that
names a package manager ("with dnf") gets that one.

## sudo

Commands that always need root (installing packages, `mount`, `mkfs`,
`systemctl start`, `useradd`, ...) get `sudo`, unless lxask runs as root, the request
is about a container, or the request says "without sudo".
