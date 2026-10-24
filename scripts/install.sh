#!/bin/sh
# Install lxask from a release: the program, the model, the encoder and the docs index.
#
#   sh install.sh                 # download the release (gh, or a token for a private repo)
#   sh install.sh --from DIR      # offline: the release files already copied into DIR
#   sh install.sh --serve | --no-serve   # answer the "keep lxask loaded?" question
#
# lxask is one program (Linux on x86_64 or ARM64, glibc 2.28 or newer: Debian 10, Ubuntu
# 18.10, RHEL 8 and later); nothing else is installed. Downloading uses gh when it is
# logged in, else curl or wget (a private repository needs gh, or GH_TOKEN with python3).
#
# Puts the model, its restriction adapter (if the release has one), the encoder, the
# docs index and their licences (NOTICE) in $LXASK_HOME (default ~/.local/share/lxask)
# and `lxask` in ~/.local/bin. It asks whether to keep lxask loaded between questions
# (recommended; default yes) and records the answer in $LXASK_HOME/settings.
set -eu

# LINU_*: the same settings as named up to 0.8, when lxask was called linu
REPO=${LXASK_REPO:-${LINU_REPO:-arxhr007/lxask}}
TAG=${LXASK_TAG:-${LINU_TAG:-v1.0.0}}
DATA=${LXASK_HOME:-${LINU_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lxask}}
BIN=${LXASK_BIN:-${LINU_BIN:-$HOME/.local/bin}}
FROM=""
TEST=1
SERVE=""  # auto or off: from --serve / --no-serve, else asked

while [ $# -gt 0 ]; do
    case $1 in
        --from) FROM=$2; shift 2 ;;
        --tag) TAG=$2; shift 2 ;;
        --no-test) TEST=0; shift ;;
        --serve) SERVE=auto; shift ;;
        --no-serve) SERVE=off; shift ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "lxask install: $*"; }
die() { printf '%s\n' "lxask install: $*" >&2; exit 1; }

command -v sha256sum >/dev/null 2>&1 || die "sha256sum not found (coreutils)"

# Which build of the program this machine runs.
case $(uname -s)/$(uname -m) in
    Linux/x86_64|Linux/amd64)
        if grep -qw avx2 /proc/cpuinfo && grep -qw fma /proc/cpuinfo; then
            PROGRAM=lxask-x86_64-v3
        elif grep -qw sse4_2 /proc/cpuinfo; then
            PROGRAM=lxask-x86_64
        else
            die "this CPU lacks SSE4.2; lxask needs an x86_64 CPU from 2009 or later"
        fi ;;
    Linux/aarch64|Linux/arm64) PROGRAM=lxask-aarch64 ;;
    *) die "lxask runs on Linux x86_64 and aarch64; this is $(uname -s) $(uname -m)" ;;
esac
if ldd --version 2>&1 | grep -qi musl; then
    die "this system uses musl libc (Alpine?); lxask's builds need glibc"
fi
GLIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null | cut -d' ' -f2)
case $GLIBC in
    "") ;;
    2.2[0-7]|2.2[0-7].*|2.1[0-9]*) die "glibc $GLIBC is too old; lxask needs 2.28 or newer" ;;
esac

WANTED="SHA256SUMS $PROGRAM embed.gguf docs.sqlite"
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
trap 'rm -rf "$STAGE"' EXIT INT TERM

http_get() {  # http_get URL OUT
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 4 --retry-delay 5 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=4 -O "$2" "$1"
    else
        return 1
    fi
}

fetch() {  # the release's files for this machine into $STAGE
    if [ -n "$FROM" ]; then
        [ -f "$FROM/SHA256SUMS" ] || die "$FROM has no SHA256SUMS: is it a copy of the release files?"
        say "copying the release files from $FROM"
        model=$(cd "$FROM" && ls lxask-*.gguf 2>/dev/null | head -n 1)
        adapter=$(cd "$FROM" && ls restrict-*.gguf 2>/dev/null | head -n 1)
        for name in $WANTED $model $adapter; do
            [ -f "$FROM/$name" ] || die "$FROM lacks $name"
            cp "$FROM/$name" "$STAGE/$name"
        done
        for name in $OPTIONAL; do
            if [ -f "$FROM/$name" ]; then cp "$FROM/$name" "$STAGE/$name"; fi
        done
    elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        say "downloading $REPO $TAG with gh (about 1.1 GB)"
        set --
        for name in $WANTED; do set -- "$@" -p "$name"; done
        gh release download "$TAG" -R "$REPO" -D "$STAGE" "$@" -p 'lxask-*.gguf' -p 'restrict-*.gguf' --clobber
        gh release download "$TAG" -R "$REPO" -D "$STAGE" -p LICENSE -p NOTICE --clobber 2>/dev/null || true
    elif [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ] && command -v python3 >/dev/null 2>&1; then
        # A private repository: assets come from the API, by id. Python reads the token
        # from the environment, so it never sits on a command line other users can see.
        say "downloading $REPO $TAG (about 1.1 GB)"
        python3 - "$REPO" "$TAG" "$STAGE" $WANTED $OPTIONAL <<'EOF' || die "download failed"
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
    if name == os.path.basename(name) and (name in wanted or (name.startswith(("lxask-", "restrict-")) and name.endswith(".gguf"))):
        print(f"  {name}", flush=True)
        get(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", os.path.join(stage, name),
            accept="application/octet-stream")
EOF
    else
        say "downloading $REPO $TAG (about 1.1 GB)"
        base="https://github.com/$REPO/releases/download/$TAG"
        http_get "$base/SHA256SUMS" "$STAGE/SHA256SUMS" \
            || die "cannot download $base/SHA256SUMS (needs curl or wget; a private repository needs gh or GH_TOKEN)"
        model=$(grep -o 'lxask-[^ ]*\.gguf' "$STAGE/SHA256SUMS" | head -n 1)
        adapter=$(grep -o 'restrict-[^ ]*\.gguf' "$STAGE/SHA256SUMS" | head -n 1)
        for name in $WANTED $model $adapter $(grep -o -w -E "LICENSE|NOTICE" "$STAGE/SHA256SUMS"); do
            [ "$name" = SHA256SUMS ] && continue
            say "  $name"
            http_get "$base/$name" "$STAGE/$name" || die "download of $name failed"
        done
    fi
}

fetch
say "checking checksums"
(cd "$STAGE" && sha256sum -c --quiet --ignore-missing SHA256SUMS) || die "checksum mismatch: the download is damaged, run again"

MODEL=$(cd "$STAGE" && ls lxask-*.gguf 2>/dev/null | head -n 1)
[ -n "$MODEL" ] && [ -f "$STAGE/$PROGRAM" ] && [ -f "$STAGE/embed.gguf" ] && [ -f "$STAGE/docs.sqlite" ] \
    || die "the release lacks one of: $PROGRAM, lxask-*.gguf, embed.gguf, docs.sqlite"
(cd "$STAGE" && grep -q " $PROGRAM\$" SHA256SUMS && grep -q " $MODEL\$" SHA256SUMS) \
    || die "SHA256SUMS does not list $PROGRAM and $MODEL"
chmod 755 "$STAGE/$PROGRAM"
"$STAGE/$PROGRAM" --version >/dev/null 2>&1 || die "$PROGRAM does not run here (glibc 2.28 or newer needed)"

# The previous files stay beside the new ones as *.previous, for going back.
for name in embed.gguf docs.sqlite; do
    if [ -f "$DATA/$name" ] && [ ! -L "$DATA/$name" ] && ! cmp -s "$DATA/$name" "$STAGE/$name"; then
        mv "$DATA/$name" "$DATA/$name.previous"
    fi
    mv "$STAGE/$name" "$DATA/$name"
done
mv "$STAGE/$MODEL" "$DATA/models/$MODEL"
PREVIOUS=$(readlink -f "$DATA/model.gguf" 2>/dev/null || true)
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$(readlink -f "$DATA/models/$MODEL")" ] && cmp -s "$PREVIOUS" "$DATA/models/$MODEL"; then
    rm -f "$PREVIOUS"  # the same model under its old name (linu-*.gguf): no second copy
elif [ -L "$DATA/model.gguf" ] && [ "$PREVIOUS" != "$(readlink -f "$DATA/models/$MODEL")" ]; then
    ln -sfn "$PREVIOUS" "$DATA/model.gguf.previous"
elif [ -f "$DATA/model.gguf" ] && [ ! -L "$DATA/model.gguf" ]; then
    mv "$DATA/model.gguf" "$DATA/model.gguf.previous"
fi
ln -sfn "$DATA/models/$MODEL" "$DATA/model.gguf"
# The adapter is made for this model: one from an older release would not fit it.
ADAPTER=$(cd "$STAGE" && ls restrict-*.gguf 2>/dev/null | head -n 1)
if [ -n "$ADAPTER" ]; then
    mv "$STAGE/$ADAPTER" "$DATA/models/$ADAPTER"
    ln -sfn "$DATA/models/$ADAPTER" "$DATA/restrict.gguf"
elif [ -L "$DATA/restrict.gguf" ]; then
    rm -f "$DATA/restrict.gguf"
fi
for name in $OPTIONAL; do
    if [ -f "$STAGE/$name" ]; then mv "$STAGE/$name" "$DATA/$name"; fi
done

mkdir -p "$BIN"
if [ -e "$BIN/lxask" ] || [ -L "$BIN/lxask" ]; then
    mv -f "$BIN/lxask" "$DATA/lxask.previous"  # an older lxask
fi
mv "$STAGE/$PROGRAM" "$BIN/lxask"
say "installed lxask ($PROGRAM) and $MODEL${ADAPTER:+ with its adapter $ADAPTER}; the command is $BIN/lxask"
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
            "It starts on your first question, holds about 1.3 GB of memory while loaded" \
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
    LXASK_HOME="$DATA" LXASK_SERVE=off "$BIN/lxask" --no-explain "find files bigger than 1GB in my home folder" 2>/dev/null \
        || die "lxask did not run; see the output above"
fi
