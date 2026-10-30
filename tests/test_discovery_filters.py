#!/usr/bin/env python3
"""Discovery must not confuse game ports with infrastructure port prefixes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "discovery", Path(__file__).resolve().parents[1] / "tools/discover-game-processes.py")
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


class DiscoveryFilters(unittest.TestCase):
    def discover(self, endpoints, *args):
        info = {"pid": 1234, "exe": "/opt/test/game-client", "uid": 1000,
                "env": {}, "comm": "game-client", "cmdline": [], "ppid": 1}
        sockets = [("tcp", "127.0.0.1:40000", endpoint) for endpoint in endpoints]
        output = io.StringIO()
        with patch.object(discovery, "socket_inodes", return_value={}), \
                patch.object(discovery, "sockets_by_pid", return_value={1234: sockets}), \
                patch.object(discovery, "proc_info", return_value=info), \
                patch.object(discovery, "ancestry", return_value=[info]), \
                patch("sys.argv", ["discover", "--json", *args]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(discovery.main(), 0)
        return [s["remote"] for r in json.loads(output.getvalue()) for s in r["sockets"]]

    def test_game_ports_with_infrastructure_prefixes_remain_visible(self):
        endpoints = ["127.0.0.1:53283", "127.0.0.1:53745", "[::1]:53530",
                     "127.0.0.1:12345", "127.0.0.1:63100", "[::1]:6800"]
        self.assertEqual(self.discover(endpoints, "--min-port", "1024"), endpoints)

    def test_exact_infrastructure_ports_are_excluded(self):
        endpoints = ["127.0.0.1:53", "[::1]:5353", "127.0.0.1:123", "[::1]:631"]
        self.assertEqual(self.discover(endpoints), [])
        self.assertEqual(self.discover(endpoints, "--all"), endpoints)

    def test_minimum_port_is_numeric_for_ipv4_and_ipv6(self):
        endpoints = ["127.0.0.1:999", "[::1]:1023", "127.0.0.1:1024", "[::1]:3724"]
        self.assertEqual(self.discover(endpoints, "--min-port", "1024"), endpoints[2:])


if __name__ == "__main__":
    unittest.main()
