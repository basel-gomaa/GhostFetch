#!/usr/bin/env bash
# Keep Actor containers off every port ON THIS HOST
# (security review N-01). Run as root:
#
#     sudo deploy/host-firewall.sh apply     # add the rules (idempotent)
#     sudo deploy/host-firewall.sh status    # show them
#     sudo deploy/host-firewall.sh remove    # take them out again, exactly
#
# WHY. A container on a Docker bridge reaches the host through the bridge's
# gateway address (172.19.0.1, 172.20.0.1, ...) and through every other host
# address. Anything listening on 0.0.0.0 -- sshd, a web server, the API if it is
# ever bound there -- is then one connect() away from a compromised Actor
# dependency. Binding the API to
# 127.0.0.1 (run-local.sh does by default) closes that path for the API only.
# This closes it for everything.
#
# WHAT IT DOES. Packets that arrive FROM the platform's bridge and are
# addressed TO the host itself go through the INPUT chain. This adds one chain
# of its own, AP-CONTAINER-INPUT, and one jump to it at the top of INPUT:
#
#     -I INPUT 1 -i ap-actor0 -j AP-CONTAINER-INPUT
#     AP-CONTAINER-INPUT: ESTABLISHED,RELATED -> RETURN   (replies to the host's own
#                         connections INTO containers, e.g. docker-proxy)
#                         tcp -> REJECT tcp-reset, rest -> REJECT icmp
#
# The bridge names are FIXED (shared/docker_net.py creates the networks with
# com.docker.network.bridge.name), and `-i <name>` matches even before the
# interface exists, so the order against Docker's own startup does not matter.
#
# WHAT IT DOES NOT TOUCH. No existing rule, no chain policy, none of Docker's
# chains, and not FORWARD -- so containers keep their outbound internet, their
# DNS (Docker forwards it to the upstream resolver, never via these bridges'
# INPUT), and the host's loopback-published ports keep working. It never flushes
# anything but its own chain. Other containers (the platform's postgres on the
# compose network, anything else you run) are unaffected.
#
# PERSISTENCE. Rules added here last until reboot. deploy/actor-platform-firewall.service
# re-applies them at boot; see actor-platform/README.md ("Host isolation").
set -euo pipefail

CHAIN="AP-CONTAINER-INPUT"
IFACES=("ap-actor0")

if [[ "$(id -u)" != "0" ]]; then
    echo "run as root: sudo $0 ${1:-status}" >&2
    exit 1
fi

# One body per family. IPv6 is not enabled on either network today, but the
# bridges still carry link-local addresses, so the same rules go on both.
apply_family() {   # $1 = iptables | ip6tables, $2 = icmp reject type
    local ipt="$1" icmp="$2"
    command -v "$ipt" >/dev/null 2>&1 || { echo "  $ipt not found, skipped"; return 0; }
    # IPv6 is belt and braces (neither network has it enabled), so a kernel
    # without a usable IPv6 filter table skips it instead of failing the whole
    # apply -- and a boot unit with it. IPv4 failing is still fatal.
    if [[ "$ipt" == ip6tables ]] && ! "$ipt" -w -S INPUT >/dev/null 2>&1; then
        echo "  ip6tables: no usable IPv6 filter table, skipped"
        return 0
    fi
    "$ipt" -w -N "$CHAIN" 2>/dev/null || true
    "$ipt" -w -F "$CHAIN"
    "$ipt" -w -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    "$ipt" -w -A "$CHAIN" -p tcp -j REJECT --reject-with tcp-reset
    "$ipt" -w -A "$CHAIN" -j REJECT --reject-with "$icmp"
    for iface in "${IFACES[@]}"; do
        "$ipt" -w -C INPUT -i "$iface" -j "$CHAIN" 2>/dev/null ||
            "$ipt" -w -I INPUT 1 -i "$iface" -j "$CHAIN"
    done
    echo "  $ipt: applied"
}

remove_family() {  # $1 = iptables | ip6tables
    local ipt="$1"
    command -v "$ipt" >/dev/null 2>&1 || return 0
    for iface in "${IFACES[@]}"; do
        while "$ipt" -w -C INPUT -i "$iface" -j "$CHAIN" 2>/dev/null; do
            "$ipt" -w -D INPUT -i "$iface" -j "$CHAIN"
        done
    done
    if "$ipt" -w -S "$CHAIN" >/dev/null 2>&1; then
        "$ipt" -w -F "$CHAIN"
        "$ipt" -w -X "$CHAIN"
    fi
    echo "  $ipt: removed"
}

status_family() {  # $1 = iptables | ip6tables
    local ipt="$1"
    command -v "$ipt" >/dev/null 2>&1 || return 0
    echo "== $ipt"
    if ! "$ipt" -w -S "$CHAIN" 2>/dev/null; then
        echo "  (not applied)"
        return 0
    fi
    "$ipt" -w -S INPUT | grep -F -- "-j $CHAIN" || echo "  (chain exists but INPUT does not jump to it)"
}

case "${1:-status}" in
    apply)
        apply_family iptables icmp-port-unreachable
        apply_family ip6tables icmp6-port-unreachable
        ;;
    remove)
        remove_family iptables
        remove_family ip6tables
        ;;
    status)
        status_family iptables
        status_family ip6tables
        ;;
    *)
        echo "usage: $0 apply|remove|status" >&2
        exit 2
        ;;
esac
