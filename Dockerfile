# The runtime these analyses run best in.
#
# It carries what pip cannot: `ccx`, which is a native executable, and a gmsh
# for 64-bit ARM Linux, which publishes no wheel for that platform and no source
# distribution. Everything else the scripts import is declared in `partcad.yaml`
# as `pythonRequirements` and installed by PartCAD into the environment it
# creates -- because this image is a *runtime*, and a package has to work in a
# conda or venv sandbox on a host that already has the native pieces.
#
# It does NOT carry this package's code. PartCAD mounts the package at run time,
# so a user who fetches a newer version gets that version rather than whatever
# was baked in months ago.
ARG PYTHON_VERSION=3.11
ARG ARCH=amd64
FROM ghcr.io/partcad/partcad-container-python:py${PYTHON_VERSION}-${ARCH}

# The moving tag, deliberately. What this image publishes is immutable and is
# what packages pin; what it is *built from* moves, so that a base image which
# breaks this plugin fails this repository's own build rather than a user's
# analysis. See "Pinning" in partcad/partcad's docs/source/configuration.rst.

LABEL partcad.image="1"
LABEL org.opencontainers.image.source="https://github.com/partcad/partcad-cae-calculix"

USER root
ENV DEBIAN_FRONTEND=noninteractive

# `calculix-ccx` is the solver. `python3-gmsh` and `libgmsh4` are the mesher:
# Debian builds them for amd64 and arm64 alike, which is the whole reason this
# image exists on ARM.
RUN apt-get update \
  && apt-get install --yes --no-install-recommends \
    calculix-ccx \
    libgmsh4 \
    python3-gmsh \
  && rm -rf /var/lib/apt/lists/*

# gmsh's Python API is a single ctypes module wrapping `libgmsh.so`, which is
# what makes this work at all: Debian installs it for *Debian's* interpreter,
# and the interpreter in this image is python.org's, in a different prefix. A
# compiled extension could not be shared across the two; a ctypes wrapper can,
# so the one file is copied somewhere both can see and named on PYTHONPATH.
#
# PYTHONPATH rather than the interpreter's own site-packages because PartCAD
# runs these scripts in a virtual environment it creates at run time, which
# starts out seeing neither.
RUN mkdir -p /opt/pc-site \
  && cp /usr/lib/python3/dist-packages/gmsh.py /opt/pc-site/gmsh.py
ENV PYTHONPATH=/opt/pc-site

# What a caller may run, extending the base image's allowlist rather than
# replacing it: the interpreter stays reachable and `ccx` becomes so.
ENV PC_CONTAINER_ALLOWED_COMMANDS='{"python": "/usr/local/bin/python3", "ccx": "/usr/bin/ccx"}'

# Two checks, and neither is redundant. The first is the base image's own,
# kept in it for exactly this: it says whether anything above broke the
# contract PartCAD talks to this image through. The second is this package's,
# and proves the two things it added are actually usable *from this
# interpreter* -- which is the assumption the copy above rests on.
COPY verify_image.py /tmp/verify_image.py
RUN python3 /pc/verify.py \
  && python3 /tmp/verify_image.py \
  && rm /tmp/verify_image.py

USER pc
