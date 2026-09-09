#!/bin/sh
#
# The tag this package's image is published and pinned under: a hash of what it
# is built from, not a version.
#
# A content tag is immutable, so a `dockerImage:` pin keeps working when the
# image is next edited, a branch that edits the image publishes a new tag rather
# than replacing the one everything else uses, and merging that branch builds
# the same tag it did.
#
# Run it to find out what to pin after changing any of the inputs below. The
# workflow runs it too, so a pin that does not match what was built fails the
# build rather than a user's analysis.
set -eu

cd "$(dirname "$0")"

# The Python version is an input as much as the Dockerfile is: it decides which
# base image this is built on, and two images built for two versions are not the
# same image however identical the Dockerfile was.
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

# sha256sum on Linux, shasum on macOS, which does not ship the former -- and the
# test suite that runs this documents macOS.
if command -v sha256sum >/dev/null 2>&1; then
    SUM="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    SUM="shasum -a 256"
else
    echo "image-tag.sh needs sha256sum or shasum" >&2
    exit 1
fi

{ echo "python ${PYTHON_VERSION}"; cat Dockerfile verify_image.py; } | ${SUM} | cut -c1-12
