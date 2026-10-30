#!/usr/bin/env python3
"""Self-contained Stage 4 UDP test using a temporary veth namespace.

The kernel sends a real UDP ping and reply through NFQUEUE. A mock shim records
the lifecycle/connect/datagram requests and emits the same nested post, remote
rule, and flow-control messages as the PE shim. With --steer, the original
outbound packet must be dropped only after its posted replacement is emitted.
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


MAGIC = 0x314C5845
VERSION = 2
HELLO, HELLO_ACK = 0x0001, 0x0002
UDP_CREATED, UDP_CREATED_REPLY = 0x0040, 0x0041
UDP_CONNECT, UDP_CONNECT_REPLY = 0x0042, 0x0043
UDP_DATAGRAM, UDP_DATAGRAM_REPLY = 0x0044, 0x0045
UDP_POST, UDP_STATE = 0x0046, 0x0047
UDP_CLOSED, UDP_CLOSED_REPLY = 0x0048, 0x0049
POST_SEND, POST_RECEIVE, ADD_REMOTE_RULE, SET_FLOWCTL = 1, 2, 3, 6
HOST_IP, PEER_IP, PEER_PORT = "192.0.2.1", "192.0.2.2", 49000
ROOT = Path(__file__).resolve().parent.parent


def receive_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("unexpected IPC EOF")
        data += chunk
    return data


def receive_frame(sock):
    magic, message_type, _flags, length = struct.unpack("<IHHI", receive_exact(sock, 12))
    if magic != MAGIC or length > 131072:
        raise RuntimeError("invalid IPC frame")
    return message_type, receive_exact(sock, length)


def send_frame(sock, message_type, payload):
    sock.sendall(struct.pack("<IHHI", MAGIC, message_type, 0, len(payload)) + payload)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def table_exists():
    return subprocess.run(
        ["/usr/bin/nft", "list", "table", "inet", "exitlag"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def echo_server():
    address = sys.argv[2]
    port = int(sys.argv[3])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind((address, port))
        payload, peer = server.recvfrom(65535)
        server.sendto(b"reply:" + payload, peer)
    return 0


def mock_shim(sock, observed, errors, closed_event, active):
    try:
        while True:
            message_type, payload = receive_frame(sock)
            if message_type in (UDP_CREATED, UDP_CLOSED):
                tag, endpoint, pid, family, local = struct.unpack("<QQIH28s", payload)
                observed.append("udpCreated" if message_type == UDP_CREATED else "udpClosed")
                reply = struct.pack("<QIIH28sB3s", tag, 0, pid, family, local, 0, bytes(3))
                send_frame(sock, UDP_CREATED_REPLY if message_type == UDP_CREATED else UDP_CLOSED_REPLY, reply)
                if message_type == UDP_CLOSED:
                    closed_event.set()
                    return
            elif message_type == UDP_CONNECT:
                tag, endpoint, pid, flag, _family, _local, remote = struct.unpack("<QQIIH28s28s", payload)
                observed.append("udpConnectRequest")
                reply = struct.pack("<QIII28sBB2s", tag, 0, pid, flag, remote, 0, 0, bytes(2))
                send_frame(sock, UDP_CONNECT_REPLY, reply)
            elif message_type == UDP_DATAGRAM:
                fixed = struct.calcsize("<QQIBBH28s28sIII")
                values = struct.unpack("<QQIBBH28s28sIII", payload[:fixed])
                tag, endpoint, _pid, direction, _reserved, _family, _local, remote, flags, options_len, data_len = values
                options = payload[fixed:fixed + options_len]
                data = payload[fixed + options_len:fixed + options_len + data_len]
                observed.append("udpSend" if direction == 2 else "udpReceive")
                api = POST_SEND if direction == 2 else POST_RECEIVE
                post = struct.pack("<QQIi28sIII", tag, endpoint, api, 0, remote, flags, len(options), len(data))
                send_frame(sock, UDP_POST, post + options + data)
                rules = controls = 0
                if direction == 2:
                    if not active:
                        send_frame(sock, UDP_STATE, struct.pack("<QQIiII", tag, endpoint, SET_FLOWCTL, 0, 3, 0))
                        controls = 1
                    remote_ip = remote[4:8]
                    rule = remote_ip[::-1] + bytes(12) + remote[2:4] + struct.pack("<H", 1)
                    send_frame(sock, UDP_STATE,
                               struct.pack("<QQIiII", tag, endpoint, ADD_REMOTE_RULE, 0, 0, len(rule)) + rule)
                    rules = 1
                done = struct.pack("<QIIIIBBBB", tag, 0, 1, rules, controls, 0, 0, 0, 0)
                send_frame(sock, UDP_DATAGRAM_REPLY, done)
    except Exception as error:
        if not closed_event.is_set():
            errors.append(error)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--echo-server":
        return echo_server()
    active = "--steer" in sys.argv[1:]
    if os.geteuid() != 0:
        print("run as root: sudo backend/test-udp-observe.py", file=sys.stderr)
        return 2
    if table_exists():
        print("refusing to touch pre-existing table inet exitlag", file=sys.stderr)
        return 3

    suffix = str(os.getpid() % 10000)
    namespace = "exlns" + suffix
    host_if = "exlh" + suffix
    peer_if = "exlp" + suffix
    ipc_port = 52000 + os.getpid() % 10000
    tcp_queue = 1000 + os.getpid() % 1000 * 2
    udp_queue = tcp_queue + 1
    namespace_created = table_installed = False
    helper = server = shim = None
    observed, errors = [], []
    closed_event = threading.Event()

    with tempfile.TemporaryDirectory(prefix="exitlag-udp-test-") as temp_dir:
        log_path = Path(temp_dir) / "netd.log"
        try:
            run("/usr/bin/ip", "netns", "add", namespace)
            namespace_created = True
            run("/usr/bin/ip", "link", "add", host_if, "type", "veth", "peer", "name", peer_if)
            run("/usr/bin/ip", "link", "set", peer_if, "netns", namespace)
            run("/usr/bin/ip", "addr", "add", HOST_IP + "/24", "dev", host_if)
            run("/usr/bin/ip", "link", "set", host_if, "up")
            run("/usr/bin/ip", "netns", "exec", namespace, "/usr/bin/ip", "link", "set", "lo", "up")
            run("/usr/bin/ip", "netns", "exec", namespace, "/usr/bin/ip", "addr", "add", PEER_IP + "/24", "dev", peer_if)
            run("/usr/bin/ip", "netns", "exec", namespace, "/usr/bin/ip", "link", "set", peer_if, "up")
            server = subprocess.Popen([
                "/usr/bin/ip", "netns", "exec", namespace, sys.executable,
                str(Path(__file__).resolve()), "--echo-server", PEER_IP, str(PEER_PORT),
            ])

            helper_args = [
                str(ROOT / "backend/exitlag-netd"), "--queue", str(tcp_queue),
                "--udp-queue", str(udp_queue), "--port", str(ipc_port),
                "--apply-udp" if active else "--observe-udp",
                *(["--udp-active-once"] if active else []),
                "--log", str(log_path),
            ]
            helper = subprocess.Popen(helper_args)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if log_path.exists() and f"bound UDP NFQUEUE {udp_queue}" in log_path.read_text(errors="replace"):
                    break
                if helper.poll() is not None:
                    raise RuntimeError("helper exited during startup")
                time.sleep(0.02)
            else:
                raise RuntimeError("helper did not bind UDP queue")

            env = os.environ.copy()
            env.update({
                "QUEUE_NUM": str(tcp_queue), "TEST_DEST": "203.0.113.1",
                "UDP_QUEUE_NUM": str(udp_queue), "UDP_TEST_DEST": PEER_IP,
                "UDP_TEST_PORT": str(PEER_PORT),
                "ENABLE_UDP_OBSERVE": "1",
            })
            if active:
                env["ENABLE_UDP_STEER"] = "1"
            run(str(ROOT / "backend/nft-setup.sh"), env=env, stdout=subprocess.DEVNULL)
            table_installed = True

            shim = socket.create_connection(("127.0.0.1", ipc_port), timeout=3)
            send_frame(shim, HELLO, struct.pack("<IIQ", VERSION, 9001, 0))
            message_type, acknowledgement = receive_frame(shim)
            if message_type != HELLO_ACK or struct.unpack("<IIQ", acknowledgement)[0] != VERSION:
                raise RuntimeError("invalid HELLO acknowledgement")
            shim.settimeout(8)
            worker = threading.Thread(
                target=mock_shim,
                args=(shim, observed, errors, closed_event, active),
                daemon=True,
            )
            worker.start()

            time.sleep(0.1)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(5)
                client.sendto(b"ping", (PEER_IP, PEER_PORT))
                reply, peer = client.recvfrom(128)
                if reply != b"reply:ping" or peer != (PEER_IP, PEER_PORT):
                    raise RuntimeError(f"bad unchanged UDP reply: {reply!r} from {peer}")

            if not closed_event.wait(6):
                raise RuntimeError("udpClosed was not observed after socket teardown")
            worker.join(1)
            if errors:
                raise errors[0]
            expected = ["udpCreated", "udpConnectRequest", "udpSend", "udpReceive", "udpClosed"]
            if observed != expected:
                raise RuntimeError(f"unexpected callback sequence: {observed}")

            log_text = log_path.read_text(errors="replace")
            required = [
                "udp udpCreated", "udp udpConnectRequest", "udp udpSend", "udp udpReceive",
                "nf_udpPostSend", "nf_udpPostReceive", "nf_udpAddRemoteAddrRule",
                "udp udpClosed",
                "remote_rules_deleted=1",
            ]
            if active:
                required += [
                    "DROP original; POST_SEND reinjected with bypass mark",
                    "ACCEPT POST_RECEIVE reply to original socket",
                ]
            else:
                required += ["nf_setUDPFlowCtl", "ACCEPT unchanged (UDP observe)"]
            missing = [text for text in required if text not in log_text]
            if missing:
                raise RuntimeError(f"missing helper evidence: {missing}")
            print("callback sequence:", " -> ".join(observed))
            print("outbound/inbound payload and nested post correlation: PASS")
            print("remote-address rule + flow-control state + implicit close deletion: PASS")
            print(f"UDP {'steering' if active else 'observation'} integration test: PASS")
        finally:
            if shim is not None:
                shim.close()
            if table_installed:
                subprocess.run([str(ROOT / "backend/nft-teardown.sh")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if helper is not None:
                helper.terminate()
                try:
                    helper.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    helper.kill(); helper.wait()
            if server is not None and server.poll() is None:
                server.terminate(); server.wait(timeout=3)
            if namespace_created:
                subprocess.run(["/usr/bin/ip", "netns", "delete", namespace],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if table_exists():
        print("teardown FAILED: table inet exitlag remains", file=sys.stderr)
        return 1
    print("teardown: PASS (no nftables table, namespace, veth, or helper)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
