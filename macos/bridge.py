#!/usr/bin/env python3
"""Experimental macOS TCP data plane for the existing EXL1 v3 Wine shim.

The default is a local test/diagnostic service, not a firewall installer.
Production interception requires explicit PF rules and --pf. UDP is not routed.
"""
import argparse
import ctypes
import fcntl
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

MAGIC = 0x314C5845
VERSION = 3
MAX_PAYLOAD = 131072
HEADER = struct.Struct("<IHHI")
HELLO = struct.Struct("<IIQ32s")
ACK = struct.Struct("<IIQ")
TCP_REQUEST = struct.Struct("<QQIIB3sH28s28s")
TCP_REPLY = struct.Struct("<QII28sBBBB")
RESOLVE_REQUEST = struct.Struct("<QII")
RESOLVE_REPLY = struct.Struct("<QII")
HELLO_MSG, ACK_MSG, PING, PONG = 1, 2, 3, 4
TCP_REQ, TCP_RESP, RESOLVE_REQ, RESOLVE_RESP = 0x10, 0x11, 0x20, 0x21


def address_bytes(endpoint):
    ip, port = endpoint
    return struct.pack("<H", 2) + struct.pack("!H", port) + socket.inet_aton(ip) + bytes(20)


def parse_address(data):
    if len(data) != 28 or struct.unpack_from("<H", data)[0] != 2:
        raise ValueError("Only IPv4 is supported by this experimental backend")
    return socket.inet_ntoa(data[4:8]), struct.unpack("!H", data[2:4])[0]


def receive_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise EOFError("IPC peer disconnected")
        data.extend(part)
    return bytes(data)


def receive_frame(sock):
    magic, kind, flags, length = HEADER.unpack(receive_exact(sock, HEADER.size))
    if magic != MAGIC or flags != 0 or length > MAX_PAYLOAD:
        raise ValueError("Invalid EXL1 frame")
    return kind, receive_exact(sock, length)


def send_frame(sock, kind, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("EXL1 payload too large")
    sock.sendall(HEADER.pack(MAGIC, kind, 0, len(payload)) + payload)


def load_identities(filename):
    result = {}
    if filename is None:
        return result
    for number, line in enumerate(Path(filename).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].startswith("/") or not parts[1].lower().endswith(".exe"):
            raise ValueError("Invalid identity map line " + str(number))
        # Standard install paths may not exist yet on a newly configured Mac.
        # Runtime attribution still requires an exact live executable match.
        source = str(Path(parts[0]).resolve())
        if source in result or "\0" in parts[1]:
            raise ValueError("Duplicate or invalid identity map line " + str(number))
        result[source] = parts[1]
    return result


class NativeIdentity:
    """Resolve only exact allowlisted executables and their actual TCP ports."""
    def __init__(self, identities):
        self.identities = identities
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib") if sys.platform == "darwin" else None
        if self.lib:
            self.lib.proc_listallpids.argtypes = (ctypes.c_void_p, ctypes.c_int)
            self.lib.proc_listallpids.restype = ctypes.c_int
            self.lib.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
            self.lib.proc_pidpath.restype = ctypes.c_int

    def path(self, pid):
        if not self.lib:
            return ""
        buf = ctypes.create_string_buffer(4096)
        if self.lib.proc_pidpath(pid, buf, len(buf)) <= 0:
            return ""
        return os.path.realpath(os.fsdecode(buf.value))

    def owner(self, local_endpoint):
        if not self.lib or not self.identities:
            return 0, ""
        count = self.lib.proc_listallpids(None, 0)
        pids = (ctypes.c_int * min(max(count + 128, 128), 131072))()
        count = self.lib.proc_listallpids(pids, ctypes.sizeof(pids))
        wanted_ip, wanted_port = local_endpoint
        for pid in list(pids)[:max(count, 0)]:
            if pid <= 0:
                continue
            exe = self.path(pid)
            identity = self.identities.get(exe)
            if not identity:
                continue
            try:
                listing = subprocess.run(
                    ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-Fn"],
                    capture_output=True, text=True, timeout=1, check=False
                ).stdout
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in listing.splitlines():
                if not line.startswith("n") or "->" not in line:
                    continue
                host, sep, port = line[1:].split("->", 1)[0].rpartition(":")
                if sep and port.isdecimal() and int(port) == wanted_port and host == wanted_ip:
                    return pid, identity
        return 0, ""


def pf_original_destination(client, listener):
    """Apple XNU pfioc_natlook: four 16-byte addresses, four 4-byte xports,
    then four one-byte fields. This is the Darwin layout, not OpenBSD's.
    See apple-oss-distributions/xnu/bsd/net/pfvar.h.
    """
    request_number = 0xC0000000 | (84 << 16) | (ord("D") << 8) | 23
    with open("/dev/pf", "r+b", buffering=0) as pf:
        for direction in (1, 2):
            query = bytearray(84)
            query[0:4] = socket.inet_aton(client[0])
            query[16:20] = socket.inet_aton(listener[0])
            query[64:66] = struct.pack("!H", client[1])
            query[68:70] = struct.pack("!H", listener[1])
            query[80:84] = bytes((socket.AF_INET, socket.IPPROTO_TCP, 0, direction))
            try:
                fcntl.ioctl(pf.fileno(), request_number, query, True)
            except OSError:
                continue
            endpoint = socket.inet_ntoa(query[48:52]), struct.unpack("!H", query[76:78])[0]
            if endpoint[1] and not ipaddress.ip_address(endpoint[0]).is_loopback:
                return endpoint
    raise OSError("PF did not return the original destination")


def write_token(path, token):
    """Never follow or replace an existing path supplied to a root process."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(token.hex())


class Peer:
    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        # ExitLag outlives helper reconnects; do not reuse its endpoint IDs.
        self.tag = time.time_ns() // 1000
        self.reported = {}
        self.last_verdict = None
        self.last_filtering_flags = None
        self.last_changed_remote = False

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def route(self, pid, identity, local, remote, timeout=0.3):
        with self.lock:
            deadline = time.monotonic() + timeout
            self.sock.settimeout(timeout)
            self.tag = max(self.tag + 1, time.time_ns() // 1000)
            tag = self.tag
            if pid and identity:
                self.reported[pid] = identity
                if len(self.reported) > 256:
                    self.reported.pop(next(iter(self.reported)))
            request = TCP_REQUEST.pack(tag, tag, pid, 0x10, 2, bytes(3), 2,
                                       address_bytes(local), address_bytes(remote))
            send_frame(self.sock, TCP_REQ, request)
            for _ in range(16):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Shim callback timed out")
                self.sock.settimeout(remaining)
                kind, data = receive_frame(self.sock)
                if kind == RESOLVE_REQ and len(data) == RESOLVE_REQUEST.size:
                    nested_tag, lookup_pid, reserved = RESOLVE_REQUEST.unpack(data)
                    if reserved:
                        raise ValueError("Invalid PID request")
                    name = self.reported.get(lookup_pid, "").encode("utf-8")
                    send_frame(self.sock, RESOLVE_RESP,
                               RESOLVE_REPLY.pack(nested_tag, len(name), 0) + name)
                    continue
                if kind == PONG and not data:
                    continue
                if kind != TCP_RESP or len(data) != TCP_REPLY.size:
                    raise ValueError("Unexpected shim reply")
                reply_tag, verdict, flags, address, changed_flags, changed_remote, r1, r2 = TCP_REPLY.unpack(data)
                if reply_tag != tag or r1 or r2 or verdict not in (0, 1, 2):
                    raise ValueError("Invalid shim reply")
                self.last_verdict = verdict
                self.last_filtering_flags = flags
                self.last_changed_remote = bool(changed_remote)
                if verdict != 1 or not changed_remote:
                    return remote
                target = parse_address(address)
                if not target[1] or not ipaddress.ip_address(target[0]).is_loopback:
                    raise ValueError("ExitLag redirect must be a loopback endpoint")
                return target
            raise ValueError("Too many nested shim messages")


class Bridge:
    def __init__(self, token, identities=None, ipc_port=51900, proxy_port=51901,
                 test_destination=None, use_pf=False, owner_lookup=None,
                 socket_adapter=None):
        self.token = token
        self.identities = NativeIdentity(identities or {})
        self.owner_lookup = owner_lookup or self.identities.owner
        self.test_destination = test_destination
        self.use_pf = use_pf
        self.socket_adapter = socket_adapter
        self.stop_event = threading.Event()
        self.peer = None
        self.peer_lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(64)
        self.ipc = self.listen(ipc_port)
        self.proxy = self.listen(proxy_port)
        self.ipc_port = self.ipc.getsockname()[1]
        self.proxy_port = self.proxy.getsockname()[1]
        self.threads = []
        self.stats_lock = threading.Lock()
        self.stats = dict(accepted=0, attributed=0, redirected=0, direct=0,
                          errors=0, bytes_sent=0, bytes_received=0,
                          relay_bytes_sent=0, relay_bytes_received=0, adapter_errors=0)
        self.flows = []

    def count(self, name, amount=1):
        with self.stats_lock:
            self.stats[name] += amount

    def snapshot(self):
        with self.stats_lock:
            result = dict(self.stats)
            result["flows"] = [dict(flow) for flow in self.flows]
        with self.peer_lock:
            result["shim_connected"] = self.peer is not None
        return result

    @staticmethod
    def listen(port):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(16)
        sock.settimeout(0.2)
        return sock

    def start(self):
        for target in (self.accept_ipc, self.accept_proxy, self.heartbeat):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def drop_peer(self, peer):
        with self.peer_lock:
            if self.peer is peer:
                self.peer = None
        peer.close()

    def accept_ipc(self):
        while not self.stop_event.is_set():
            try:
                sock, address = self.ipc.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                sock.settimeout(0.5)
                kind, payload = receive_frame(sock)
                if kind != HELLO_MSG or len(payload) != HELLO.size:
                    raise ValueError("Expected exact HELLO")
                version, pid, caps, token = HELLO.unpack(payload)
                if version != VERSION or not hmac.compare_digest(token, self.token):
                    raise ValueError("IPC authentication failed")
                with self.peer_lock:
                    if self.peer is not None:
                        raise ValueError("A shim is already connected")
                    send_frame(sock, ACK_MSG, ACK.pack(VERSION, os.getpid(), 0))
                    self.peer = Peer(sock)
            except (OSError, EOFError, ValueError, struct.error):
                sock.close()

    def heartbeat(self):
        while not self.stop_event.wait(2):
            with self.peer_lock:
                peer = self.peer
            if not peer or not peer.lock.acquire(blocking=False):
                continue
            try:
                peer.sock.settimeout(0.5)
                send_frame(peer.sock, PING)
                kind, payload = receive_frame(peer.sock)
                if kind != PONG or payload:
                    raise ValueError("Invalid heartbeat")
            except (OSError, EOFError, ValueError):
                self.drop_peer(peer)
            finally:
                peer.lock.release()

    def select_route(self, pid, identity, local, original):
        if not pid or not identity:
            return original
        with self.peer_lock:
            peer = self.peer
        if peer is None:
            return original
        try:
            return peer.route(pid, identity, local, original)
        except (OSError, EOFError, ValueError, struct.error):
            self.drop_peer(peer)
            return original

    def connect_route(self, pid, identity, source_ip, original):
        """Keep the TCP source tuple announced to the relay.

        A userspace proxy creates a second TCP socket. Announcing the game's
        source port and then connecting from a new port loses the relay's flow
        association. Reserve this socket before the callback, and connect that
        same socket if ExitLag redirects it. Attribution still uses the original
        game socket, never the proxy socket.
        """
        if self.socket_adapter:
            try:
                return self.socket_adapter.connect(self, pid, identity, source_ip, original)
            except (OSError, EOFError, ValueError, struct.error):
                self.count("adapter_errors")
                return socket.create_connection(original, timeout=5), False
        reserved = socket.socket()
        try:
            reserved.bind((source_ip, 0))
            target = self.select_route(pid, identity, reserved.getsockname(), original)
            if target != original:
                reserved.settimeout(5)
                reserved.connect(target)
                return reserved, True
        except BaseException:
            reserved.close()
            raise
        reserved.close()
        return socket.create_connection(original, timeout=5), False

    def accept_proxy(self):
        while not self.stop_event.is_set():
            try:
                client, local = self.proxy.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.capacity.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self.handle_client, args=(client, local), daemon=True).start()

    def handle_client(self, client, local):
        remote = None
        with self.stats_lock:
            self.stats["accepted"] += 1
            flow = dict(id=self.stats["accepted"], opened_at=time.time(),
                        active=True, bytes_sent=0, bytes_received=0)
            self.flows.append(flow)
            self.flows = [item for item in self.flows if item["active"] or item in self.flows[-16:]]
        stage = "destination_lookup"
        try:
            if self.test_destination:
                original = self.test_destination
            elif self.use_pf:
                original = pf_original_destination(local, client.getsockname())
            else:
                return
            stage = "process_attribution"
            pid, identity = self.owner_lookup(local)
            with self.stats_lock:
                flow.update(pid=pid, original=f"{original[0]}:{original[1]}")
            if pid and identity:
                self.count("attributed")
            stage = "relay_connect"
            remote, is_relay = self.connect_route(pid, identity, local[0], original)
            with self.stats_lock:
                flow["relay"] = is_relay
            self.count("redirected" if is_relay else "direct")
            stage = "transfer"
            # Both local forwarding legs carry latency-sensitive small game
            # packets. Do not introduce Nagle buffering between proxies.
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            remote.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # A character-selection socket can legitimately be quiet for minutes.
            # Poll for readability so shutdown remains responsive, without
            # turning an idle receive into a forced game disconnect.
            client.settimeout(30)
            remote.settimeout(30)

            def copy(source, destination, counter):
                try:
                    while not self.stop_event.is_set():
                        readable, _, _ = select.select([source], [], [], 1)
                        if not readable:
                            continue
                        data = source.recv(65536)
                        if not data:
                            with self.stats_lock:
                                flow[counter + "_end"] = "eof"
                                flow[counter + "_end_at"] = time.time()
                            break
                        destination.sendall(data)
                        self.count(counter, len(data))
                        with self.stats_lock:
                            flow[counter] += len(data)
                            flow["last_data_at"] = time.time()
                        if is_relay:
                            self.count("relay_" + counter, len(data))
                except (OSError, ValueError) as error:
                    with self.stats_lock:
                        flow[counter + "_end"] = type(error).__name__
                        flow[counter + "_errno"] = getattr(error, "errno", None)
                        flow[counter + "_end_at"] = time.time()
                        self.stats["errors"] += 1
                finally:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            incoming = threading.Thread(target=copy, args=(remote, client, "bytes_received"), daemon=True)
            incoming.start()
            copy(client, remote, "bytes_sent")
            incoming.join(timeout=65)
        except (OSError, ValueError) as error:
            with self.stats_lock:
                self.stats["errors"] += 1
                self.stats["last_error"] = {"stage": stage, "type": type(error).__name__,
                                            "errno": getattr(error,"errno",None)}
        finally:
            with self.stats_lock:
                flow.update(active=False, closed_at=time.time())
            client.close()
            if remote:
                remote.close()
            self.capacity.release()

    def close(self):
        self.stop_event.set()
        self.ipc.close()
        self.proxy.close()
        with self.peer_lock:
            peer, self.peer = self.peer, None
        if peer:
            peer.close()
        for thread in self.threads:
            thread.join(timeout=1)


def endpoint(value):
    host, port = value.rsplit(":", 1)
    ip = ipaddress.IPv4Address(host)
    if not 0 < int(port) < 65536:
        raise ValueError("Invalid port")
    return str(ip), int(port)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--identity-map", type=Path)
    parser.add_argument("--ipc-port", type=int, default=51900)
    parser.add_argument("--proxy-port", type=int, default=51901)
    parser.add_argument("--metrics-file", type=Path)
    parser.add_argument("--companion-config", type=Path)
    parser.add_argument("--companion-uid", type=int)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--pf", action="store_true", help="Read original destinations from PF; does not install rules")
    modes.add_argument("--test-destination", type=endpoint, help="Local loopback echo target for tests")
    args = parser.parse_args()
    if args.pf and (sys.platform != "darwin" or os.geteuid() != 0):
        parser.error("--pf requires macOS and root")
    if args.test_destination and not ipaddress.ip_address(args.test_destination[0]).is_loopback:
        parser.error("Test mode permits loopback destinations only")
    identities = load_identities(args.identity_map)
    if args.pf and not identities:
        parser.error("PF mode requires an explicit executable identity map")
    token = secrets.token_bytes(32)
    write_token(args.token_file, token)
    service = None
    try:
        adapter = None
        if args.companion_config:
            from wine_socket import WineSocketAdapter
            adapter = WineSocketAdapter.load(args.companion_config, args.companion_uid,
                                            identities.values())
        service = Bridge(token, identities, args.ipc_port, args.proxy_port,
                         args.test_destination, args.pf, socket_adapter=adapter)
        service.start()
        def stop(signum, frame):
            service.stop_event.set()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        print(json.dumps({"ready": True, "ipc": service.ipc_port, "proxy": service.proxy_port,
                          "mode": "pf-tcp-experimental" if args.pf else "local-test"}), flush=True)
        while not service.stop_event.wait(1):
            if args.metrics_file:
                args.metrics_file.write_text(json.dumps(service.snapshot()) + "\n")
    finally:
        if service:
            service.close()
        args.token_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
