#!/bin/sh
# Install lxask from a release: the program, the model, the encoder and the docs index.
#
#   sh install.sh                 # download the release (gh, or a token for a private repo)
#   sh install.sh --from DIR      # offline: the release files already copied into DIR
#   sh install.sh --serve | --no-serve   # answer the "keep lxask loaded?" question
#   sh install.sh --skip-memory-check   # install with under 1.3 GB of memory anyway
#   sh install.sh --program FILE  # a program you built (cargo build), not the release's
#
# lxask is one program (Linux on x86_64 or ARM64, glibc 2.28 or newer: Debian 10, Ubuntu
# 18.10, RHEL 8 and later); nothing else is installed. Releases have no macOS program:
# on a Mac, build it and pass --program; the model and the rest come from the release.
# Downloading uses gh when it is
# logged in, else curl or wget (a private repository needs gh, or GH_TOKEN with python3).
#
# Puts the model, its adapters (restrict-* and precision-*, if the release has them),
# the encoder, the docs index and their licences (NOTICE) in $LXASK_HOME (default
# ~/.local/share/lxask)
# and `lxask` in ~/.local/bin. It asks whether to keep lxask loaded between questions
# (recommended; default yes) and records the answer in $LXASK_HOME/settings.
set -eu

# LINU_*: the same settings as named up to 0.8, when lxask was called linu
REPO=${LXASK_REPO:-${LINU_REPO:-arxhr007/lxask}}
TAG=${LXASK_TAG:-${LINU_TAG:-v1.5.8}}  # release_files.sh checks this is the version released
DATA=${LXASK_HOME:-${LINU_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lxask}}
BIN=${LXASK_BIN:-${LINU_BIN:-$HOME/.local/bin}}
FROM=""
LOCAL=""  # --program: a program built here, used instead of the release's
TEST=1
SERVE=""  # auto or off: from --serve / --no-serve, else asked
MEMCHECK=1

while [ $# -gt 0 ]; do
    case $1 in
        --from) FROM=$2; shift 2 ;;
        --tag) TAG=$2; shift 2 ;;
        --program) LOCAL=$2; shift 2 ;;
        --no-test) TEST=0; shift ;;
        --skip-memory-check) MEMCHECK=0; shift ;;
        --serve) SERVE=auto; shift ;;
        --no-serve) SERVE=off; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "lxask install: $*"; }
die() { printf '%s\n' "lxask install: $*" >&2; exit 1; }

# sha256sum is coreutils; macOS has shasum instead, which takes the same file and flags.
if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$@"; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$@"; }
else
    die "sha256sum not found (coreutils)"
fi
# Two files with the same contents. cmp is in diffutils, which minimal systems lack.
same() {
    [ -f "$1" ] && [ -f "$2" ] || return 1
    if command -v cmp >/dev/null 2>&1; then
        cmp -s "$1" "$2"
    else
        [ "$(sha256 < "$1")" = "$(sha256 < "$2")" ]
    fi
}
# timeout is coreutils too; without it the old server is stopped without a time limit.
with_timeout() {  # with_timeout SECONDS COMMAND...
    if command -v timeout >/dev/null 2>&1; then
        timeout "$@"
    else
        shift
        "$@"
    fi
}

if [ -n "$LOCAL" ]; then
    [ -f "$LOCAL" ] || die "--program $LOCAL: no such file"
    "$LOCAL" --version >/dev/null 2>&1 || die "--program $LOCAL does not run here"
fi

# Which build of the program this machine runs.
case $(uname -s)/$(uname -m) in
    Darwin/*)
        # No release has a macOS program: it is built here, before 1.1 GB is downloaded.
        [ -n "$LOCAL" ] || die "releases have no macOS program; build it and pass it with --program:
    cargo build --release --manifest-path rust/Cargo.toml -p lxask
    sh scripts/install.sh --program rust/target/release/lxask"
        PROGRAM=lxask-macos-$(uname -m) ;;
    Linux/x86_64|Linux/amd64)
        if grep -qw avx2 /proc/cpuinfo && grep -qw fma /proc/cpuinfo; then
            PROGRAM=lxask-x86_64-v3
        elif grep -qw sse4_2 /proc/cpuinfo; then
            PROGRAM=lxask-x86_64
        else
            die "this CPU lacks SSE4.2; lxask needs an x86_64 CPU from 2009 or later"
        fi ;;
    Linux/aarch64|Linux/arm64)
        # Dot product and half-precision arithmetic (ARMv8.2: Raspberry Pi 5, Graviton, Apple,
        # most ARM servers) make reading the prompt much faster than the plain ARMv8 build
        if grep -qw asimddp /proc/cpuinfo 2>/dev/null && grep -qw asimdhp /proc/cpuinfo 2>/dev/null; then
            PROGRAM=lxask-aarch64-dotprod
            # a directory of release files from before that build existed
            if [ -n "$FROM" ] && [ ! -f "$FROM/$PROGRAM" ] && [ -f "$FROM/lxask-aarch64" ]; then
                PROGRAM=lxask-aarch64
            fi
        else
            PROGRAM=lxask-aarch64
        fi ;;
    Linux/arm*) die "this is a 32-bit ARM system ($(uname -m)); lxask needs a 64-bit one (on a Raspberry Pi 4 or 5: Raspberry Pi OS (64-bit))" ;;
    *) die "lxask runs on Linux x86_64 and aarch64 and on macOS; this is $(uname -s) $(uname -m)" ;;
esac
# A 32-bit system on a 64-bit kernel (32-bit Raspberry Pi OS on a Pi 4 or 5 is one)
# reports aarch64 above, but cannot run a 64-bit program
if [ "$(getconf LONG_BIT 2>/dev/null)" = 32 ]; then
    die "this is a 32-bit system on a 64-bit kernel; lxask needs a 64-bit one (on a Raspberry Pi: Raspberry Pi OS (64-bit))"
fi
# The model takes about 1.3 GB of memory while it answers: a board with 1 GB cannot hold
# it, and swap is far too slow to make up the difference. A container's limit counts.
MEM_KB=$(sed -n 's/^MemTotal: *\([0-9]*\) kB$/\1/p' /proc/meminfo 2>/dev/null || true)
for limit in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes; do
    bytes=$(cat "$limit" 2>/dev/null || true)
    case $bytes in
        ''|*[!0-9]*) ;;
        *) if [ -n "$MEM_KB" ] && [ $((bytes / 1024)) -lt "$MEM_KB" ]; then MEM_KB=$((bytes / 1024)); fi ;;
    esac
done
if [ "$MEMCHECK" = 1 ] && [ -n "$MEM_KB" ] && [ "$MEM_KB" -lt 1310720 ]; then
    die "this machine has $((MEM_KB / 1024)) MB of memory; lxask needs about 1.3 GB free while it answers (a Raspberry Pi needs 2 GB or more). --skip-memory-check installs anyway"
fi
# The release's Linux programs need glibc; a program built here was built for this libc.
if [ "$(uname -s)" = Linux ] && [ -z "$LOCAL" ]; then
    if ldd --version 2>&1 | grep -qi musl; then
        die "this system uses musl libc (Alpine?); lxask's builds need glibc"
    fi
    GLIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null | cut -d' ' -f2)
    case $GLIBC in
        "") ;;
        2.2[0-7]|2.2[0-7].*|2.1[0-9]*) die "glibc $GLIBC is too old; lxask needs 2.28 or newer" ;;
    esac
fi

OPTIONAL="LICENSE NOTICE"  # releases before 0.8.1 have none

# Installed as linu (0.8 and earlier): its files move here once, links included.
OLD=${XDG_DATA_HOME:-$HOME/.local/share}/linu
if [ -z "${LXASK_HOME:-${LINU_HOME:-}}" ] && [ ! -e "$DATA" ] && [ -d "$OLD" ] && [ ! -L "$OLD" ]; then
    say "moving $OLD to $DATA (linu is now called lxask)"
    mv "$OLD" "$DATA"
    for link in "$DATA"/*.gguf "$DATA"/*.previous; do
        [ -L "$link" ] || continue
        target=$(readlink "$link")
        case $target in "$OLD"/*) ln -sfn "$DATA${target#"$OLD"}" "$link" ;; esac
    done
fi
mkdir -p "$DATA/models"
STAGE=$(mktemp -d "$DATA/.download.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
trap 'exit 130' INT TERM  # through the EXIT trap; a bare cleanup trap would carry on

http_get() {  # http_get URL OUT
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 4 --retry-delay 5 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=4 -O "$2" "$1"
    else
        return 1
    fi
}

# Where the release's files come from: a copy (--from), gh, the API with a token, or a URL.
if [ -n "$FROM" ]; then
    METHOD=copy
elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    METHOD=gh
elif [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ] && command -v python3 >/dev/null 2>&1; then
    METHOD=api
else
    METHOD=url
fi

fetch() {  # fetch NAME...: these files of the release into $STAGE
    [ $# -gt 0 ] || return 0
    case $METHOD in
    copy)
        for name in "$@"; do
            [ -f "$FROM/$name" ] || die "$FROM lacks $name"
            cp "$FROM/$name" "$STAGE/$name"
        done ;;
    gh)
        for name in "$@"; do set -- "$@" -p "$name"; shift; done
        gh release download "$TAG" -R "$REPO" -D "$STAGE" "$@" --clobber || die "download failed" ;;
    api)
        # A private repository: assets come from the API, by id. Python reads the token
        # from the environment, so it never sits on a command line other users can see.
        python3 - "$REPO" "$TAG" "$STAGE" "$@" <<'EOF' || die "download failed"
import json, os, sys, time, urllib.error, urllib.request

repo, tag, stage, *wanted = sys.argv[1:]
token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the asset's storage URL is fetched without the token


def get(url, out=None, accept=None, auth=True):
    headers = {"User-Agent": "lxask-install"}
    if auth:
        headers["Authorization"] = "Bearer " + token
    if accept:
        headers["Accept"] = accept
    opener = urllib.request.build_opener(NoRedirect) if auth else urllib.request.build_opener()
    for attempt in range(4):
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
                if out is None:
                    return response.read()
                total, done, shown = int(response.headers.get("Content-Length") or 0), 0, time.time()
                with open(out, "wb") as file:
                    while chunk := response.read(1 << 20):
                        file.write(chunk)
                        done += len(chunk)
                        if total > 50 << 20 and time.time() - shown > 10:
                            print(f"    {done >> 20} of {total >> 20} MB", flush=True)
                            shown = time.time()
                # a connection that closes early ends the reads without an error
                if total and done != total:
                    raise OSError(f"got {done} of {total} bytes")
                return None
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                return get(error.headers["Location"], out, auth=False)
            if error.code in (401, 403, 404):
                sys.exit(f"lxask install: {url}: {error}")
            failure = error
        except OSError as error:
            failure = error
        print(f"    retrying after: {failure}", flush=True)
        time.sleep(5 * (attempt + 1))
    sys.exit(f"lxask install: {url}: {failure}")


release = json.loads(get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}"))
for asset in release["assets"]:
    name = asset["name"]
    if name == os.path.basename(name) and name in wanted:
        print(f"  {name}", flush=True)
        get(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", os.path.join(stage, name),
            accept="application/octet-stream")
EOF
        ;;
    url)
        base="https://github.com/$REPO/releases/download/$TAG"
        for name in "$@"; do
            [ "$name" = SHA256SUMS ] || say "  $name"
            http_get "$base/$name" "$STAGE/$name" \
                || die "cannot download $base/$name (needs curl or wget; a private repository needs gh or GH_TOKEN)"
        done ;;
    esac
}

# SHA256SUMS first: it names the release's files, and says which installed ones are the same.
if [ "$METHOD" = copy ]; then
    [ -f "$FROM/SHA256SUMS" ] || die "$FROM has no SHA256SUMS: is it a copy of the release files?"
    say "copying the release files from $FROM"
else
    say "downloading the list of $REPO $TAG's files"
fi
fetch SHA256SUMS
[ -f "$STAGE/SHA256SUMS" ] || die "the release has no SHA256SUMS"
listed_sum() {  # listed_sum NAME: its checksum in SHA256SUMS ("*NAME" is binary mode)
    awk -v name="$1" '{ file = $2; sub(/^\*/, "", file) } file == name { print $1; exit }' "$STAGE/SHA256SUMS"
}
listed() {  # listed PATTERN: the first file in SHA256SUMS whose name matches
    awk '{ file = $2; sub(/^\*/, "", file); print file }' "$STAGE/SHA256SUMS" | grep -x -E "$1" | head -n 1
}
MODEL=$(listed 'lxask-.*\.gguf')
RESTRICT=$(listed 'restrict-.*\.gguf')
PRECISION=$(listed 'precision-.*\.gguf')
EXTRA="$(listed LICENSE) $(listed NOTICE)"  # releases before 0.8.1 have neither

# An upgrade often keeps the model (940 MB) and changes only the program and the index.
# An installed file whose checksum is the release's is the same file: kept where it is,
# not downloaded again.
installed_path() {
    case $1 in
        embed.gguf|docs.sqlite) printf '%s\n' "$DATA/$1" ;;
        *) printf '%s\n' "$DATA/models/$1" ;;
    esac
}
unchanged() {  # unchanged NAME: the installed copy is the release's file
    file=$(installed_path "$1")
    want=$(listed_sum "$1")
    [ -f "$file" ] && [ -n "$want" ] && [ "$(sha256 < "$file" | cut -d' ' -f1)" = "$want" ]
}
KEPT=""
NEED=""
if [ -n "$MODEL" ] && [ -f "$(installed_path "$MODEL")" ] || [ -f "$DATA/docs.sqlite" ]; then
    say "checking which installed files are unchanged"
fi
for name in embed.gguf docs.sqlite $MODEL $RESTRICT $PRECISION; do
    if unchanged "$name"; then KEPT="$KEPT $name"; else NEED="$NEED $name"; fi
done
KEPT=${KEPT# }
kept() { case " $KEPT " in *" $1 "*) return 0 ;; esac; return 1; }
present() { [ -f "$STAGE/$1" ] || kept "$1"; }
[ -n "$KEPT" ] && say "unchanged, kept as installed: $KEPT"
if [ -n "$LOCAL" ]; then
    NEED="$NEED $EXTRA"  # the program is $LOCAL
else
    NEED="$PROGRAM $NEED $EXTRA"
fi
if [ "$METHOD" != copy ]; then
    if [ -n "$MODEL" ] && kept "$MODEL"; then
        say "downloading the rest of $REPO $TAG"
    else
        if [ "$METHOD" = gh ]; then via=" with gh"; else via=""; fi
        say "downloading $REPO $TAG$via (about 1.1 GB)"
    fi
fi
# shellcheck disable=SC2086  # NEED is a list of file names, which have no spaces
fetch $NEED
say "checking checksums"
(cd "$STAGE" && sha256 -c --quiet --ignore-missing SHA256SUMS) || die "checksum mismatch: the download is damaged, run again"

if [ -n "$LOCAL" ]; then
    # Built here, so the release's SHA256SUMS cannot list it; copied in after the check.
    cp "$LOCAL" "$STAGE/$PROGRAM"
    LISTED=$MODEL
else
    LISTED="$PROGRAM $MODEL"
fi
[ -n "$MODEL" ] && present "$MODEL" && [ -f "$STAGE/$PROGRAM" ] && present embed.gguf && present docs.sqlite \
    || die "the release lacks one of: $PROGRAM, lxask-*.gguf, embed.gguf, docs.sqlite"
for name in $LISTED; do
    (cd "$STAGE" && grep -q " $name\$" SHA256SUMS) || die "SHA256SUMS does not list $name"
done
chmod 755 "$STAGE/$PROGRAM"
"$STAGE/$PROGRAM" --version >/dev/null 2>&1 || die "$PROGRAM does not run here (glibc 2.28 or newer needed)"

# The previous files stay beside the new ones as *.previous, for going back.
for name in embed.gguf docs.sqlite; do
    kept "$name" && continue  # already in place
    if [ -f "$DATA/$name" ] && [ ! -L "$DATA/$name" ] && ! same "$DATA/$name" "$STAGE/$name"; then
        mv "$DATA/$name" "$DATA/$name.previous"
    fi
    mv "$STAGE/$name" "$DATA/$name"
done
kept "$MODEL" || mv "$STAGE/$MODEL" "$DATA/models/$MODEL"
PREVIOUS=$(readlink -f "$DATA/model.gguf" 2>/dev/null || true)
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$(readlink -f "$DATA/models/$MODEL")" ] && same "$PREVIOUS" "$DATA/models/$MODEL"; then
    rm -f "$PREVIOUS"  # the same model under its old name (linu-*.gguf): no second copy
elif [ -L "$DATA/model.gguf" ] && [ "$PREVIOUS" != "$(readlink -f "$DATA/models/$MODEL")" ]; then
    ln -sfn "$PREVIOUS" "$DATA/model.gguf.previous"
elif [ -f "$DATA/model.gguf" ] && [ ! -L "$DATA/model.gguf" ]; then
    mv "$DATA/model.gguf" "$DATA/model.gguf.previous"
fi
ln -sfn "$DATA/models/$MODEL" "$DATA/model.gguf"
# The adapters are made for this model: ones from an older release would not fit it.
ADAPTER=""
for kind in restrict precision; do
    if [ $kind = restrict ]; then name=$RESTRICT; else name=$PRECISION; fi
    if [ -n "$name" ] && present "$name"; then
        kept "$name" || mv "$STAGE/$name" "$DATA/models/$name"
        ln -sfn "$DATA/models/$name" "$DATA/$kind.gguf"
        ADAPTER="$ADAPTER $name"
    elif [ -L "$DATA/$kind.gguf" ]; then
        rm -f "$DATA/$kind.gguf"
    fi
done
ADAPTER=${ADAPTER# }
for name in $OPTIONAL; do
    if [ -f "$STAGE/$name" ]; then mv "$STAGE/$name" "$DATA/$name"; fi
done

mkdir -p "$BIN"
if [ -e "$BIN/lxask" ] || [ -L "$BIN/lxask" ]; then
    mv -f "$BIN/lxask" "$DATA/lxask.previous"  # an older lxask
fi
mv "$STAGE/$PROGRAM" "$BIN/lxask"
say "installed lxask (${LOCAL:-$PROGRAM}) and $MODEL${ADAPTER:+ with its adapters $ADAPTER}; the command is $BIN/lxask"
# A server the old lxask started holds the old model and answers no newer program:
# stopped, so the next question starts one of this version (any version obeys a stop).
LXASK_HOME="$DATA" with_timeout 30 "$BIN/lxask" --stop 2>&1 | grep -q 'server stopped' \
    && say "stopped the lxask server the old version had running"
if [ -e "$BIN/linu" ] && [ ! -L "$BIN/linu" ]; then
    mv -f "$BIN/linu" "$DATA/linu.previous"
    ln -s lxask "$BIN/linu"
    say "linu is now called lxask; the linu command still works (a link to lxask)"
fi
if [ -d "$DATA/venv" ]; then
    say "the Python environment of linu 0.5 and earlier is no longer used: rm -r $DATA/venv"
fi
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "add $BIN to PATH, e.g. echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile" ;;
esac

# Faster answers: with serve = auto, the first question starts lxask in the background
# and later ones ask it (about 2 s instead of 7); it stops itself after 30 idle minutes.
SETTINGS="$DATA/settings"
if [ -z "$SERVE" ] && grep -q '^[[:space:]]*serve[[:space:]]*=' "$SETTINGS" 2>/dev/null; then
    SERVE=keep  # asked on an earlier install
fi
if [ -z "$SERVE" ]; then
    SERVE=auto
    if (: </dev/tty) 2>/dev/null; then
        printf '%s\n%s\n%s' \
            "Keep lxask loaded between questions, for answers in about 2 s instead of 7?" \
            "It starts on your first question, holds about 1.4 GB of memory while loaded" \
            "and stops itself after 30 minutes without a question. Recommended. [Y/n] " >/dev/tty
        reply=""
        read -r reply </dev/tty || reply=""
        case $reply in [nN]*) SERVE=off ;; esac
    fi
fi
if [ "$SERVE" != keep ]; then
    {
        [ -f "$SETTINGS" ] || printf '# lxask settings, written by install.sh\n'
        grep -v '^[[:space:]]*serve[[:space:]]*=' "$SETTINGS" 2>/dev/null || true
        printf 'serve = %s\n' "$SERVE"
    } > "$SETTINGS.new"
    mv "$SETTINGS.new" "$SETTINGS"
fi
case $(sed -n 's/^[[:space:]]*serve[[:space:]]*=[[:space:]]*//p' "$SETTINGS" | tail -n 1) in
    auto) say "lxask stays loaded between questions (to change: serve = off in $SETTINGS)" ;;
    *) say "lxask loads for each question (for faster answers: serve = auto in $SETTINGS)" ;;
esac

if [ "$TEST" = 1 ]; then
    say "trying it once:"
    # its notes on stderr only matter when it fails
    LXASK_HOME="$DATA" LXASK_SERVE=off "$BIN/lxask" --no-explain "find files bigger than 1GB in my home folder" 2>"$STAGE/try.err" \
        || { cat "$STAGE/try.err" >&2; die "lxask did not run; see the output above"; }
fi
