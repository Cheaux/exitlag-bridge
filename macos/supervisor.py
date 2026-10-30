#!/usr/bin/env python3
"""Review a narrow PF session; --apply additionally requires administrator access."""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import time
from pf_plan import make_rules
from bridge import load_identities

def stop_requested(path, uid):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > 16:
                return True
            return os.read(fd, 17).strip() != b"run"
        finally:
            os.close(fd)
    except OSError:
        return True

def command(*args):
    return subprocess.run(list(args),text=True,capture_output=True,check=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid",required=True,type=int)
    parser.add_argument("--identity-map",required=True,type=Path)
    parser.add_argument("--target",required=True,action="append")
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--companion-config",type=Path)
    args=parser.parse_args()
    identities=load_identities(args.identity_map)
    if not identities:parser.error("An exact executable identity map is required")
    rules=make_rules(args.uid,51901,args.target)
    print(rules,flush=True)
    if not args.apply:
        print("Review only. No firewall rules were loaded.")
        return
    if sys.platform!="darwin" or os.geteuid()!=0:
        parser.error("--apply requires macOS and administrator access")
    if "SUDO_UID" in os.environ and int(os.environ["SUDO_UID"])!=args.uid:
        parser.error("--uid must be the invoking user's UID")
    here=Path(__file__).resolve().parent
    runtime=Path("/var/run")/f"exitlag-macos-{args.uid}"
    runtime.mkdir(mode=0o711)  # refuse an existing, unowned or stale session
    anchor=f"com.apple/cheaux.exitlag.{args.uid}.{os.getpid()}"
    token=runtime/"session.token"
    anchor_file=runtime/"anchor.conf"
    control=runtime/"control"
    metrics=runtime/"stats.json"
    state_file=runtime/"state.json"
    lease_file=runtime/"pf-enable.token"
    anchor_file.write_text(rules)
    anchor_file.chmod(0o600)
    child=None
    enable_token=None
    anchor_loaded=False
    def interrupted(signum,frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGHUP,interrupted)
    try:
        for path, data in ((control,"run\n"),(metrics,"{}\n")):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
            with os.fdopen(fd,"w") as output:output.write(data)
            os.chown(path,args.uid,-1)
        bridge_command=[sys.executable,"-u",str(here/"bridge.py"),"--pf",
            "--token-file",str(token),"--metrics-file",str(metrics),
            "--identity-map",str(args.identity_map.resolve())]
        if args.companion_config:
            bridge_command.extend(("--companion-config",str(args.companion_config.resolve()),
                                   "--companion-uid",str(args.uid)))
        child=subprocess.Popen(bridge_command,
            stdout=subprocess.PIPE,text=True)
        ready,_,_=select.select([child.stdout],[],[],10)
        if not ready:raise RuntimeError("Bridge did not become ready")
        status=json.loads(child.stdout.readline())
        if not status.get("ready"):raise RuntimeError("Bridge startup failed")
        os.chown(token,args.uid,-1)
        # Preserve other PF users' references. Never reload /etc/pf.conf.
        previous=command("/sbin/pfctl","-s","info")
        if not re.search(r"Status:\s*Enabled",previous.stdout):
            enabled=command("/sbin/pfctl","-E")
            match=re.search(r"Token\s*:\s*(\d+)",enabled.stdout+enabled.stderr)
            if not match:raise RuntimeError("PF did not return an enable reference token")
            enable_token=match.group(1)
            lease_file.write_text(enable_token+"\n")
            lease_file.chmod(0o600)
        command("/sbin/pfctl","-a",anchor,"-f",str(anchor_file))
        anchor_loaded=True
        state_file.write_text(json.dumps({"active":True,"anchor":anchor,"pid":os.getpid(),"uid":args.uid,"targets":args.target})+"\n")
        state_file.chmod(0o600)
        os.chown(state_file,args.uid,-1)
        print("Experimental IPv4 TCP session active. Leave this terminal open.",flush=True)
        print("Wine token file: "+str(token),flush=True)
        print("Use Stop WoW Routing.command to end the test and remove its rules.",flush=True)
        while child.poll() is None and not stop_requested(control,args.uid):
            time.sleep(.25)
        if child.poll() is None:return
        if child.returncode:raise RuntimeError("Bridge stopped unexpectedly")
    except KeyboardInterrupt:
        pass
    finally:
        # A second terminal-close signal must not interrupt cleanup halfway.
        for signum in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):
            signal.signal(signum,signal.SIG_IGN)
        if anchor_loaded:
            subprocess.run(["/sbin/pfctl","-a",anchor,"-F","all"],capture_output=True)
        if enable_token:
            subprocess.run(["/sbin/pfctl","-X",enable_token],capture_output=True)
        if child and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if child and child.stdout:child.stdout.close()
        token.unlink(missing_ok=True)
        anchor_file.unlink(missing_ok=True)
        control.unlink(missing_ok=True)
        metrics.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)
        lease_file.unlink(missing_ok=True)
        runtime.rmdir()

if __name__=="__main__":
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
