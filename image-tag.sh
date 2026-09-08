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
cat Dockerfile verify_image.py | sha256sum | cut -c1-12
