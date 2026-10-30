#!/usr/bin/env python3
"""Print an explicit TCP PF anchor without activating it."""
import argparse
import ipaddress

def make_rules(uid, proxy_port, targets):
    if not 500 <= uid <= 2147483647:
        raise ValueError("Use the game user's UID, never root or a system account")
    if not 1024 <= proxy_port <= 65535:
        raise ValueError("Proxy port must be an unprivileged TCP port")
    redirects, routes = [], []
    for target in sorted(set(targets)):
        host, port = target.rsplit(":", 1)
        network, port = ipaddress.IPv4Network(host, strict=False), int(port)
        if (network.prefixlen < 8 or network.is_loopback or network.is_multicast
                or network.is_unspecified or not 0 < port <= 65535):
            raise ValueError("Expected a specific non-loopback IPv4 network and TCP port")
        redirects.append(f"rdr pass on lo0 inet proto tcp from any to {network} port {port} -> 127.0.0.1 port {proxy_port}")
        routes.append(f"pass out quick on ! lo0 route-to (lo0 127.0.0.1) inet proto tcp from any to {network} port {port} user {uid} flags S/SA keep state")
    if not redirects:
        raise ValueError("At least one explicit destination is required")
    return "\n".join(["# Experimental ExitLag macOS TCP bridge", *redirects, *routes, ""])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--proxy-port", type=int, default=51901)
    parser.add_argument("--target", action="append", required=True, help="IPv4 or CIDR:TCP_PORT")
    args = parser.parse_args()
    try:
        print(make_rules(args.uid, args.proxy_port, args.target), end="")
    except (ValueError, TypeError) as error:
        parser.error(str(error))

if __name__ == "__main__":
    main()
