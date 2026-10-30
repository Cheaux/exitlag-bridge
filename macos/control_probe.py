#!/usr/bin/env python3
"""Test the real Wine shim's local IPC without changing firewall or game traffic."""
import argparse
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import time
from bridge import Bridge, endpoint, load_identities, write_token


def game_flows(identity):
    """Read existing IPv4 flows belonging to allowlisted native games."""
    result = []
    count = identity.lib.proc_listallpids(None, 0)
    pids = (ctypes.c_int * min(max(count + 128, 128), 131072))()
    count = identity.lib.proc_listallpids(pids, ctypes.sizeof(pids))
    for pid in list(pids)[:max(count, 0)]:
        name = identity.identities.get(identity.path(pid))
        if not name:
            continue
        listing = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-Fn"],
            capture_output=True, text=True, timeout=2, check=False)
        for line in listing.stdout.splitlines():
            if not line.startswith("n") or "->" not in line:
                continue
            try:
                local, remote = line[1:].split("->", 1)
                local_ip, local_port = local.rsplit(":", 1)
                remote_ip, remote_port = remote.rsplit(":", 1)
                ipaddress.IPv4Address(local_ip)
                address = ipaddress.IPv4Address(remote_ip)
                if address.is_loopback:
                    continue
                result.append((pid, name, (local_ip, int(local_port)), (remote_ip, int(remote_port))))
            except ValueError:
                continue
    priority = {3724: 0, 6724: 1, 1119: 2, 443: 3}
    return sorted(result, key=lambda flow: priority.get(flow[3][1], 4))


def game_flow(identity):
    flows = game_flows(identity)
    return flows[0] if flows else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--identity-map", required=True, type=Path)
    parser.add_argument("--timeout", default=600, type=int)
    parser.add_argument("--probe-game-decision", action="store_true")
    parser.add_argument("--probe-relay-greeting", action="store_true",
                        help="Open one extra TCP connection, receive a greeting only; no game interception")
    parser.add_argument("--probe-target", type=endpoint,
                        help="Previously observed game server, when the character is currently disconnected")
    args = parser.parse_args()
    if os.geteuid() == 0 or not 1 <= args.timeout <= 900:
        parser.error("Run as the game user, with a timeout of 1–900 seconds")
    identities = load_identities(args.identity_map)
    token = secrets.token_bytes(32)
    write_token(args.token_file, token)
    service = None
    state = {"firewall_changed": False, "game_traffic_redirected": False,
             "ready": False, "shim_connected": False, "callback_complete": False}

    def save():
        args.status_file.write_text(json.dumps(state, indent=2) + "\n")
        args.status_file.chmod(0o600)

    try:
        # No packets are accepted from the game: the proxy has no PF steering.
        # The loopback discard address is unused; only IPC is probed here.
        service = Bridge(token, identities, test_destination=("127.0.0.1", 9))
        service.start()
        state["ready"] = True
        save()
        def stop(signum, frame):
            service.stop_event.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        deadline = time.monotonic() + args.timeout
        connected_at = None
        attempted = False
        while time.monotonic() < deadline and not service.stop_event.wait(0.25):
            with service.peer_lock:
                peer = service.peer
            if peer is None:
                connected_at = None
                if state["shim_connected"]:
                    state["shim_connected"] = False
                    save()
                continue
            if connected_at is None:
                connected_at = time.monotonic()
                state["shim_connected"] = True
                save()
            if time.monotonic() - connected_at < 5:
                continue
            if not state.get("connection_stable"):
                state["connection_stable"] = True
                save()
            if args.probe_relay_greeting and not attempted:
                flows = game_flows(service.identities)
                if not args.probe_target:
                    flows = [flow for flow in flows if flow[3][1] == 3724]
                if flows:
                    attempted = True
                    pid, name, local, original = flows[0]
                    original = args.probe_target or original
                    state["eligible_game_target"] = {"ip": original[0], "port": original[1], "pid": pid}
                    # Count greetings only. Never send credentials or save payloads.
                    for mode in ("direct", "relay"):
                        try:
                            if mode == "direct":
                                sock = socket.create_connection(original, timeout=5)
                                redirected = False
                            else:
                                sock, redirected = service.connect_route(pid, name, local[0], original)
                            with sock:
                                sock.settimeout(10)
                                count = len(sock.recv(256))
                            state[mode + "_greeting_bytes"] = count
                            if mode == "relay":
                                state["callback_redirected"] = redirected
                                state["callback_verdict"] = peer.last_verdict
                                state["callback_complete"] = peer.last_verdict != 2
                        except (OSError, EOFError, ValueError) as error:
                            state[mode + "_error"] = type(error).__name__
                        save()
                    state["relay_probe_complete"] = True
                    save()
            elif args.probe_game_decision and not attempted:
                flows = game_flows(service.identities)
                if flows:
                    attempted = True
                    results = []
                    for pid, name, local, original in flows[:12]:
                        try:
                            target = peer.route(pid, name, local, original, timeout=3)
                            state.update(callback_complete=peer.last_verdict != 2,
                                         callback_verdict=peer.last_verdict,
                                         callback_redirected=target != original)
                            results.append({"destination_port": original[1],
                                            "verdict": peer.last_verdict,
                                            "flags": peer.last_filtering_flags,
                                            "redirected": target != original})
                            if target != original:
                                state["eligible_game_target"] = {"ip": original[0], "port": original[1], "pid": pid}
                                state["relay_loopback_port"] = target[1]
                                break
                        except (OSError, EOFError, ValueError) as error:
                            state["callback_error"] = type(error).__name__
                            break
                    state["decisions"] = results
                    save()
    finally:
        if service:
            service.close()
        args.token_file.unlink(missing_ok=True)
        state["stopped"] = True
        save()


if __name__ == "__main__":
    main()
