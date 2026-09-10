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

# The mesher, from Debian, which builds `python3-gmsh` for amd64 and arm64
# alike -- which is the whole reason this image exists on ARM.
#
# The gmsh shared library is deliberately not named. It is soname-versioned --
# `libgmsh4.11` on one Debian release and `libgmsh4.13` on the next -- so
# naming it pins this image to a release rather than to a package. That is how
# `libgmsh4` got here: it is not a name any Debian has, and apt said so. It does
# not need naming either way, because `python3-gmsh` is a ctypes wrapper that
# cannot work without the library and therefore depends on whichever one it
# needs; apt resolves that better than this file can.
#
# The solver was installed here too, as `calculix-ccx`, and is not any more:
# the base image moved to Debian 13 (trixie), which has no such package. apt
# offers no installation candidate for it, and the only name left matching
# `calculix` is `calculix-ccx-test`, which is documentation. It comes from
# conda-forge below instead.
#
# A package that cannot be installed says what this Debian *does* offer before
# it fails. Without that, apt exits 100 and the log leaves you unable to tell a
# renamed package from a dropped one -- which is how the paragraph above got
# written, and the failure this step is most likely to have again, since what
# it installs from is the base image's Debian and that moves when the base does.
RUN apt-get update \
  && { apt-get install --yes --no-install-recommends python3-gmsh \
       || { echo "=== apt cannot install what this image is for ==="; \
            . /etc/os-release && echo "base: ${PRETTY_NAME}"; \
            apt-cache policy python3-gmsh || true; \
            apt-cache search --names-only '^libgmsh' || true; \
            exit 1; }; } \
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

# The solver, from conda-forge, which builds `ccx` for `linux-64` and
# `linux-aarch64` -- the two architectures this image is published for -- and
# is already one of the three ways this package tells a user to get a solver
# (see `calculix_common.find_ccx()`).
#
# It goes into a prefix of its own rather than into /usr. The conda package is
# a single executable, and everything it links against beyond libc -- arpack, a
# BLAS, libgfortran -- is reached through that binary's own RPATH inside the
# prefix. So the closure is guaranteed consistent with the binary that needs
# it, and none of it lands on a search path where it could shadow Debian's.
#
# The solver's version is pinned, and micromamba is fetched by version and
# checked against the hash conda-forge publishes, so that "an image built from
# this file" keeps meaning roughly one thing -- `image-tag.sh` names it by a
# hash of this file. Roughly, not exactly: the build number and the closure
# below it float, the way the base image and the Debian packages above already
# do. micromamba and its package cache are removed in the layer that unpacks
# them; what stays is the prefix.
#
# Python does the downloading because this base image has neither curl nor
# bzip2, and installing both to unpack one file would leave both here.
RUN set -eu; \
    MICROMAMBA_VERSION=2.9.0; \
    CALCULIX_VERSION=2.23; \
    case "$(uname -m)" in \
      x86_64) SUBDIR=linux-64; SHA256=8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd ;; \
      aarch64) SUBDIR=linux-aarch64; SHA256=e705ffeed90ce0659eb546e4b1e1028c9eaf0bc9cc854867b19ac5ce0ba5852f ;; \
      *) echo "this image pins no micromamba for $(uname -m)" >&2; exit 1 ;; \
    esac; \
    python3 -c 'import hashlib, io, os, sys, tarfile, urllib.request; url, want = sys.argv[1], sys.argv[2]; blob = urllib.request.urlopen(url).read(); got = hashlib.sha256(blob).hexdigest(); got == want or sys.exit("%s hashes to %s, not the pinned %s" % (url, got, want)); open("/usr/local/bin/micromamba", "wb").write(tarfile.open(fileobj=io.BytesIO(blob), mode="r:bz2").extractfile("bin/micromamba").read()); os.chmod("/usr/local/bin/micromamba", 0o755)' \
      "https://conda.anaconda.org/conda-forge/${SUBDIR}/micromamba-${MICROMAMBA_VERSION}-0.tar.bz2" "${SHA256}"; \
    micromamba create --yes --root-prefix /tmp/mamba --prefix /opt/calculix \
      --channel conda-forge "calculix=${CALCULIX_VERSION}"; \
    rm -rf /tmp/mamba /usr/local/bin/micromamba

# Where every way of looking finds it: `ccx` on PATH, and in /usr/local/bin,
# which `calculix_common.find_ccx()` searches even when PATH says nothing. A
# symlink rather than an edit to PATH, because PartCAD builds a sandbox inside
# this container and a sandbox rebuilds PATH.
RUN ln -s /opt/calculix/bin/ccx /usr/local/bin/ccx

# What a caller may run, extending the base image's allowlist rather than
# replacing it: the interpreter stays reachable and `ccx` becomes so.
ENV PC_CONTAINER_ALLOWED_COMMANDS='{"python": "/usr/local/bin/python3", "ccx": "/usr/local/bin/ccx"}'

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
