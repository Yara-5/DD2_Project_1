#!/bin/sh
# Netgen from the course image, for Tutorial 3's flow.py: NETGEN=src/netgen.sh.
#
# Tutorial 3's tools.sh starts Netgen as "netgen-lvs", Ubuntu's package name for it;
# the course image (student pack container/) builds Netgen from source as "netgen",
# so tools.sh cannot start it there. flow.py runs the command named by $NETGEN
# instead of tools.sh, so this runs the same container with the same mounts as
# tools.sh: the Tutorial 3 folder ($TUT3_ROOT) at its own path, and the PDK that its
# pdk link points to, read-only, at the link's target.
set -e
ROOT=$(cd "${TUT3_ROOT:?set TUT3_ROOT to the Tutorial 3 folder}" && pwd)
SKY130=$(cd "$ROOT/pdk" && pwd -P)
LINK_TARGET=$(readlink "$ROOT/pdk")
case "$LINK_TARGET" in
    /*) PDK_MOUNT="$LINK_TARGET" ;;
    *)  PDK_MOUNT="$ROOT/pdk" ;;
esac
exec ${DOCKER:-docker} run --rm -i \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -v "$ROOT:$ROOT" \
    -v "$SKY130:$PDK_MOUNT:ro" \
    -w "$(pwd)" \
    --entrypoint netgen \
    "${LAYOUT_IMAGE:-dd2-layout-tools}" "$@"
