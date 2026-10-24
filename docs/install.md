# Install

## Requirements

- Linux on x86_64 or ARM64 (ARMv8-A: Raspberry Pi 4 and 5, ARM servers).
- glibc 2.28 or newer: Debian 10, Ubuntu 18.10, Fedora, RHEL 8, Arch, openSUSE and
  later. Alpine and other musl systems are not supported.
- About 1.1 GB of disk and 1.3 GB of free memory while answering.
- Nothing else: no Python, no compiler, no GPU. After installing, no network.

## Platforms

| platform | build | tested |
|---|---|---|
| x86_64 with AVX2 (most PCs since 2015) | `lxask-x86_64-v3` | yes: Ubuntu 24.04 (WSL2) and Debian/Ubuntu containers, every release; CI on each push |
| older x86_64 (SSE4.2) | `lxask-x86_64` | yes, the same way, less often |
| ARM64 (Raspberry Pi 4/5, ARM servers) | `lxask-aarch64` | yes: installs and answers the same as x86_64; reports from real devices welcome |
| other distributions (Fedora, Arch, openSUSE) | same builds | the program runs anywhere with glibc 2.28+; answers are tuned and measured on Debian/Ubuntu ([accuracy.md](accuracy.md#platforms)) |

## Install or upgrade

```sh
curl -fsSLO https://github.com/arxhr007/lxask/releases/latest/download/install.sh
sh install.sh
```

(`wget https://github.com/arxhr007/lxask/releases/latest/download/install.sh` works
too.) To check the script before running it, against the release's checksums:

```sh
curl -fsSLO https://github.com/arxhr007/lxask/releases/latest/download/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS      # install.sh: OK
```

The script:

1. picks the build for your CPU: `lxask-x86_64-v3` (AVX2, nearly every x86 PC since
   2015), `lxask-x86_64` (older x86) or `lxask-aarch64`;
2. downloads it with the model, its adapter, the encoder and the documentation
   index, using curl, wget or a logged-in `gh`;
3. checks every file against `SHA256SUMS`;
4. puts `lxask` in `~/.local/bin` and the rest in `~/.local/share/lxask`;
5. asks whether to keep lxask loaded between questions, for answers in about 2 s
   instead of 7 (recommended, the default; without a terminal to ask in, yes);
6. asks one question to prove it runs.

It needs no root and writes nothing else: no system files, no shell startup files, no
services. Upgrading from linu also moves `~/.local/share/linu` (below).

If `~/.local/bin` is not on your `PATH`, it says how to add it.

Running it again upgrades. The previous program is kept as
`~/.local/share/lxask/lxask.previous` and the previous model as `model.gguf.previous`.

Options: `--tag v1.0.0` installs a given release, `--serve` / `--no-serve` answer
the question above, `--no-test` skips the test question, and `LXASK_HOME` or `LXASK_BIN` install somewhere else.

## Without a network

On a machine with a network, download every file of a release from its GitHub page
into one directory. Copy that directory to the other machine and run:

```sh
sh install.sh --from DIR
```

## Check it

```sh
lxask --version
lxask "show disk usage of each folder here"
```

The first answer takes a few seconds while the model loads. To keep it loaded, see
[the server](usage.md#keep-the-model-loaded).

## Go back to the previous version

```sh
mv ~/.local/share/lxask/lxask.previous ~/.local/bin/lxask
ln -sfn "$(readlink -f ~/.local/share/lxask/model.gguf.previous)" ~/.local/share/lxask/model.gguf
```

Or run an older release's own installer, from its page on GitHub (`--tag` works only
between releases from 0.9 on: before that the program was called linu and its files
are named so).

## Upgrading from linu (0.8 and earlier)

lxask was called linu up to version 0.8. Installing 0.9 or later over it moves
`~/.local/share/linu` to `~/.local/share/lxask`, keeps one copy of the model (the
same model, under its new name), and leaves `linu` working as a link to `lxask`. The
old `LINU_*` environment variables are still read; the `LXASK_*` names come first.

## Uninstall

```sh
lxask --stop                      # if a server is running
rm ~/.local/bin/lxask
rm -r ~/.local/share/lxask        # the model, the index, your feedback reports
```

## Problems

- **"this CPU lacks SSE4.2"** or **"glibc ... is too old"**: the machine is older
  than lxask supports.
- **"this system uses musl libc"**: Alpine. Use a Debian or Ubuntu container instead.
- **"checksum mismatch"**: the download was damaged. Run the installer again.
- **Slow answers**: about 7 seconds on a recent laptop is normal. `lxask --serve`
  brings it to about 2. On a small ARM board, expect several times that.
- **Out of memory**: lxask needs about 1.3 GB free. Close other programs, or stop a
  `lxask --serve` you no longer need with `lxask --stop`.
