# Source this from WSL before running uv: `source scripts/env.sh`
# The repo lives on /mnt/c, where small-file I/O is slow, so the virtualenv
# and the model caches are kept on the Linux filesystem instead.
export PATH="$HOME/.local/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/lxask-dev"
export HF_HOME="$HOME/.cache/huggingface"
