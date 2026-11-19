---
title: lxask
emoji: 🐧
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Linux commands from plain words, checked against man pages
models:
  - arxhr007/lxask
---

# lxask on Hugging Face

The demo of [lxask](https://github.com/arxhr007/lxask), an offline Linux command
assistant: ask in plain words, get the command, explained part by part from the man
pages and tldr, with what the documentation cannot confirm marked `unverified:`.

The Space builds the `lxask` program from source (`LXASK_REF` in the Dockerfile) and
takes the model, its two adapters, the encoder and the docs index from a GitHub release
(`LXASK_TAG`), checked against the release's SHA256SUMS. The answers are the ones the
program gives on your own machine, where it runs offline:

```sh
curl -fsSLO https://github.com/arxhr007/lxask/releases/latest/download/install.sh
sh install.sh
```

This folder lives in the lxask repository as `space/`; see `space/DEPLOY.md` there.
