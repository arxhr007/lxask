# Deploying the Space

The Space at `huggingface.co/spaces/arxhr007/lxask` is this folder: `Dockerfile`,
`app.py`, `requirements.txt` and `README.md` (whose front matter configures it).
`DEPLOY.md` stays here.

## Before uploading

Pin what it runs, in the `Dockerfile`:

- `LXASK_REF`: the commit of github.com/arxhr007/lxask to build. Use a full commit
  hash, not `main`, so a rebuild of the Space runs the same program. It must be a
  commit with `lxask --explain`.
- `LXASK_TAG`: the release whose model files to use, normally the latest.

Try it locally first:

```sh
docker build -t lxask-space space/
docker run --rm -p 7860:7860 lxask-space     # then open http://localhost:7860
```

## Uploading

Once, create the Space at https://huggingface.co/new-space (owner arxhr007, name
lxask, SDK Docker, blank template, the free CPU) and log in:

```sh
pip install -U huggingface_hub
hf auth login                                   # a token with write access
```

Then, each time:

```sh
hf upload arxhr007/lxask space . --repo-type space --exclude DEPLOY.md
```

The Space rebuilds on every upload: about 15 minutes, most of it compiling llama.cpp
and downloading the 1.1 GB of model files.

## Settings

- `LXASK_THREADS` (a Space variable): CPU threads for the model; by default the CPUs
  the container may use, at most 8.
- Free Spaces sleep after 48 hours without a visitor; the first question after that
  waits for the model to load again.
