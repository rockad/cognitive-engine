#!/bin/sh
# Build (or confirm) the contained-proposer session image on the verified rootless Docker daemon.
# Every docker call is pinned to the socket the rootless gate verified; nothing is pulled or built at run time.
#
#   packages/harness2/containers/proposer/build.sh          build, then print the tag and image id
#   packages/harness2/containers/proposer/build.sh status   only check that the image for the current sources exists
set -eu
cd "$(dirname "$0")/../../../.."
exec uv run python -m cognitive_harness2.image "${1:-build}"
