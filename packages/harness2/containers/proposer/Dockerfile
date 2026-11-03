# Contained proposer session image (RFC 004 6.2.3, slice 3b).
#
# One image serves both containers of a session: the SESSION container (the agent CLI is bind-mounted
# read-only at run time; this image carries only a shell, coreutils and CA certificates) and the PROXY
# container (python3 plus the allow-list proxy copied in below).
#
# The base is pinned by DIGEST. `cognitive_harness2.image` refuses a Dockerfile whose FROM is not, and
# derives the image tag from a hash of this file and the proxy source, so a tag always names its source.
# To rebuild: `packages/harness2/containers/proposer/build.sh` (see the README, "Contained proposer sessions").
FROM ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3-minimal libpython3-stdlib \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt /var/log/dpkg.log /var/log/alternatives.log \
           /usr/share/doc/* /usr/share/man/* /usr/share/info/* /usr/share/lintian/* \
 && find /usr/lib/python3* -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null; true

RUN install -d -m 0755 /opt/harness2
COPY --chmod=0644 egress_proxy.py /opt/harness2/egress_proxy.py

# Unprivileged by default; the runner passes --user and HOME explicitly as well.
USER 65534:65534
WORKDIR /tmp
CMD ["/bin/sh"]
