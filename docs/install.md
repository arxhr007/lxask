# Install

## Requirements

- 64-bit Linux on x86_64 or ARM64 (ARMv8-A: Raspberry Pi 4 and 5, ARM servers).
- glibc 2.28 or newer: Debian 10, Ubuntu 18.10, Fedora, RHEL 8, Arch, openSUSE and
  later. Alpine and other musl systems are not supported.
- About 1.1 GB of disk and 1.3 GB of free memory while answering (up to 1.5 GB on
  ARM boards: a Raspberry Pi needs 2 GB of RAM or more).
- Nothing else: no Python, no compiler, no GPU. After installing, no network.

## Platforms

| platform | build | tested |
|---|---|---|
| x86_64 with AVX2 (most PCs since 2015) | `lxask-x86_64-v3` | yes: Ubuntu 24.04 (WSL2) and Debian/Ubuntu containers, every release; CI on each push |
| older x86_64 (SSE4.2) | `lxask-x86_64` | yes, the same way, less often |
| ARM64 with dot product (Raspberry Pi 5 and CM5, Graviton, Apple, most ARM servers) | `lxask-aarch64-dotprod` | installs and answers the same as x86_64 (checked under emulation); speed measured on a real Pi 5 pending |
| other ARM64 (Raspberry Pi 4) | `lxask-aarch64` | yes: installs and answers the same as x86_64; reports from real devices welcome |
| other distributions (Fedora, Arch, openSUSE) | same builds | the program runs anywhere with glibc 2.28+; answers are tuned and measured on Debian/Ubuntu ([accuracy.md](accuracy.md#platforms)) |
| macOS (Apple Silicon or Intel) | built from source, below | builds, passes the tests and answers on an Apple Silicon Mac; answers are still Linux (GNU) commands |

### macOS

Releases have no macOS program, so it is built on the Mac (needs Rust, from
[rustup.rs](https://rustup.rs), and the Xcode command line tools). install.sh then puts
it in place and downloads the model and the rest from the release as usual:

```sh
cargo build --release --manifest-path rust/Cargo.toml -p lxask
sh scripts/install.sh --program rust/target/release/lxask
```

lxask still answers with Linux commands: GNU options such as `du --max-depth` or
`ls --directory` work on a Mac only with GNU coreutils (`brew install coreutils`). It is
most useful on a Mac for commands you will run on Linux machines and containers.

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
   2015), `lxask-x86_64` (older x86), `lxask-aarch64-dotprod` (ARM64 with the dot-product
   instructions, which `/proc/cpuinfo` lists as `asimddp`) or `lxask-aarch64`;
2. downloads it with the model, its adapters, the encoder and the documentation
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

Options: `--tag v1.0.0` installs a given release (an older one, say), `--serve` / `--no-serve` answer
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

## Raspberry Pi and other small boards

| board | runs lxask? |
|---|---|
| Raspberry Pi 5 and CM5, 2 GB or more | yes, with `lxask-aarch64-dotprod` |
| Raspberry Pi 4 and CM4, 2 GB or more | yes, with `lxask-aarch64`, more slowly: its CPU has no dot-product instructions |
| Raspberry Pi 3, Zero 2 W, any 1 GB board | no: the model does not fit in 1 GB, and the installer stops before downloading it |
| Raspberry Pi 2, Zero, 1 | no: 32-bit CPUs |

- **A 64-bit system.** Install Raspberry Pi OS (64-bit). The 32-bit Raspberry Pi OS
  runs a 64-bit kernel on a Pi 4 or 5, so `uname -m` says `aarch64`, but it cannot run
  a 64-bit program: the installer checks `getconf LONG_BIT` and stops there.
  `getconf LONG_BIT` prints 64 on a system lxask runs on.
- **Keep it loaded.** Most of the time goes into reading the notes for each question,
  and loading the model adds several seconds more, so answer yes to keeping lxask
  loaded. The server stops after 30 minutes without a question and gives its memory
  back. On a 2 GB board, other programs have about half a gigabyte while it runs.
- **Speed.** Measured by a user on a CM5 ([accuracy.md](accuracy.md#raspberry-pi)):
  16.5 s per question with the plain ARM build of 1.2.0, 7.0 s with lxask built on the
  board for its CPU. `lxask-aarch64-dotprod` uses the same faster instructions; its
  time on a real board is not measured yet, and reports are welcome on
  [GitHub](https://github.com/arxhr007/lxask/issues) (`LXASK_TIMING=1 lxask --serve`
  prints where each answer's time went).
- **All four cores.** Since 1.5.1 lxask writes the reply with one thread per core, so
  all four on a Pi; earlier versions used two (half the CPUs, which on a PC with
  hyperthreads is one per core). On four separate cores of a PC, four threads wrote
  replies 1.4 times as fast as two. Reading the prompt, the larger part of an answer,
  already used every core, so a whole answer gains less. `--threads N` sets it by hand.
- **Cooling.** Under a long load a Pi without a cooler slows itself down when it gets
  hot; `vcgencmd get_throttled` prints `throttled=0x0` when it has not.
- `--no-rag` answers without the notes: on the CM5 3.0 s instead of 16.5 s. The
  accuracy numbers are measured with the notes.

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
- **"this is a 32-bit system"** or **"a 32-bit ARM system"**: install a 64-bit system
  (on a Raspberry Pi, Raspberry Pi OS (64-bit)).
- **"this machine has ... MB of memory"**: under 1.3 GB, the model does not fit.
  `--skip-memory-check` installs anyway, for a machine whose memory is about to grow.
- **"checksum mismatch"**: the download was damaged. Run the installer again.
- **Slow answers**: about 7 seconds on a recent laptop is normal. `lxask --serve`
  brings it to about 2. On a small ARM board, expect several times that.
- **Out of memory**: lxask needs about 1.3 GB free. Close other programs, or stop a
  `lxask --serve` you no longer need with `lxask --stop`.
