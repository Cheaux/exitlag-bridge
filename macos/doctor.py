#!/usr/bin/env python3
"""Read-only macOS port diagnostics. Does not enable PF or modify a prefix."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wine", type=Path)
    parser.add_argument("--prefix", type=Path)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    results = {"platform":sys.platform,"python":sys.version.split()[0],
        "native_tcp_bridge":(here/"bridge.py").is_file(),
        "windows_nfapi_built":(here/"build/nfapi.dll").is_file(),
        "windows_driver_stub_built":(here/"build/nfextlag.sys").is_file(),
        "pfctl":shutil.which("pfctl"),
        "administrator":os.geteuid()==0,
        "udp_routing":False,
        "live_exitlag_validation":"not performed"}
    if sys.platform=="darwin":
        try:
            ctypes.CDLL("/usr/lib/libproc.dylib")
            results["native_process_attribution"]=True
        except OSError:
            results["native_process_attribution"]=False
    if args.wine:
        try:
            p=subprocess.run([str(args.wine),"--version"],capture_output=True,text=True,timeout=5)
            results["wine_version"]=p.stdout.strip() if p.returncode==0 else "failed"
        except (OSError,subprocess.TimeoutExpired):
            results["wine_version"]="unavailable"
    if args.prefix:
        results["prefix_initialized"]=(args.prefix/"system.reg").is_file()
        programs=args.prefix/"drive_c"
        results["official_client_present"]=(programs/"ExitLag/ExitLag.exe").is_file() or any(programs.glob("Program Files*/**/ExitLag.exe"))
    print(json.dumps(results,indent=2))
    print("\nPF plan generation and loopback tests do not prove live game routing.")
    print("The official client needs a valid account and compatible Wine WMI support.")

if __name__=="__main__":
    main()
