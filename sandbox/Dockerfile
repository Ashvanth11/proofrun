# The one image every sandboxed command runs in.
#
# Built once (see ai_monitor/agent/sandbox.py:IMAGE) and reused for every repo:
# each tool call is a fresh `docker run --rm` against a per-repo volume, so the
# image must carry everything an install might need before the network is taken
# away. Python-only for batch 1 - repos in other languages come back as
# `could_not_run` with the `unsupported_language` blocker, which is a finding,
# not a gap.
FROM python:3.12-slim

# git to clone, build-essential because a surprising number of requirements
# still compile C extensions, curl to prove in the integration test that
# `--network none` actually bites.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Containers run as --user 1000:1000 with a read-only root filesystem, so every
# path the tooling writes to has to be either the mounted volume or a tmpfs.
# pip's cache and HOME both default under /home, which is neither.
ENV HOME=/tmp \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# The one line that stops a whole class of false verdicts.
#
# site-packages is unwritable under --read-only, so pip quietly falls back to a
# user-site install and still exits 0. User-site derives from HOME, which is the
# tmpfs - so `pip install -e .` in a setup call reports success and is gone by
# the next container, and the agent sees import errors and concludes the repo's
# claims are refuted. Pointing PYTHONUSERBASE at the volume puts that fallback
# somewhere that survives, and python adds it to sys.path on its own.
ENV PYTHONUSERBASE=/work/.local

# uid 1000 is not in python:3.12-slim's passwd; some tools (git included) are
# unhappy about an unknown uid, so give it a name and a home under /tmp.
RUN useradd --uid 1000 --no-create-home --home-dir /tmp --shell /bin/bash sandbox

# The per-repo named volume is mounted here. Docker seeds an empty named volume
# from the image's contents at the mount point, ownership included, so creating
# /work owned by uid 1000 here is what makes the volume writable by a container
# running as --user 1000:1000 with --cap-drop ALL (no CAP_CHOWN to fix it later).
RUN mkdir -p /work && chown 1000:1000 /work
WORKDIR /work
