#!/usr/bin/env python3
"""Start a reviewed foreground routing test, or request its unprivileged stop."""
import argparse
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from pf_plan import make_rules


def request_stop(runtime, uid):
    try:
        fd=os.open(runtime/"control",os.O_WRONLY|getattr(os,"O_NOFOLLOW",0))
    except FileNotFoundError:
        return False
    with os.fdopen(fd,"wb") as output:
        info=os.fstat(output.fileno())
        if info.st_uid!=uid or not stat.S_ISREG(info.st_mode):
            raise SystemExit("Unexpected session control file")
        output.write(b"stop\n")
        output.truncate()
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("start","stop"))
    parser.add_argument("--uid",type=int,default=os.getuid())
    parser.add_argument("--target",action="append",default=[])
    parser.add_argument("--identity-map",type=Path)
    parser.add_argument("--probe-directory",type=Path)
    parser.add_argument("--replace",action="store_true",
                        help="End an existing test before starting the reviewed replacement")
    parser.add_argument("--wine-sockets",action="store_true",
                        help="Use a user-owned Wine socket adapter for native Classic Era")
    args=parser.parse_args()
    if os.geteuid()==0 or args.uid!=os.getuid():
        parser.error("Run this launcher as the game user; it requests sudo only for the scoped supervisor")
    runtime=Path("/var/run")/f"exitlag-macos-{args.uid}"
    if args.action=="stop":
        if not request_stop(runtime,args.uid):
            print("No routing test is active.")
            return
        print("Stop requested. The supervisor will remove its rules and close the bridge.")
        return
    if not args.identity_map:
        parser.error("--identity-map is required for start")
    rules=make_rules(args.uid,51901,args.target)
    if runtime.exists():
        if not args.replace:
            raise SystemExit("A session already exists. Run Stop WoW Routing.command first.")
        request_stop(runtime,args.uid)
        deadline=time.monotonic()+8
        while runtime.exists() and time.monotonic()<deadline:
            time.sleep(.1)
        if runtime.exists():
            import json
            from recover import pid_exists, validate_state
            state=json.loads((runtime/"state.json").read_text())
            validate_state(state,args.uid)
            try:
                live=pid_exists(state["pid"])
            except PermissionError:
                live=True
            if live:
                raise SystemExit("The previous supervisor is still running; replacement was not started")
            print("The previous supervisor has exited. Administrator access is needed to remove only its stale routing anchor.",flush=True)
            recovery=Path(__file__).resolve().parent/"recover.py"
            subprocess.run(["/usr/bin/sudo",sys.executable,str(recovery),"--uid",str(args.uid)],check=True)
            if runtime.exists():
                raise SystemExit("The previous routing session could not be recovered")
    print("Temporary IPv4 TCP test for this user and these destinations only:\n"+rules,flush=True)
    print("The next prompt is your Mac admin password, entered directly into sudo.",flush=True)
    print("After the test starts, reconnect your WoW character to create a new game connection.",flush=True)
    alias=None
    alias_inode=None
    if args.probe_directory:
        folder=args.probe_directory.resolve()
        pid_file=folder/"probe.pid"
        if pid_file.exists():
            pid=int(pid_file.read_text().strip())
            command=subprocess.run(["/bin/ps","-p",str(pid),"-o","args="],capture_output=True,text=True).stdout
            owner=subprocess.run(["/bin/ps","-p",str(pid),"-o","uid="],capture_output=True,text=True).stdout.strip()
            if "macos/control_probe.py" in command and owner==str(args.uid):
                os.kill(pid,signal.SIGTERM)
                for _ in range(50):
                    if not (folder/"session.token").exists():break
                    time.sleep(.1)
        alias=folder/"session.token"
        if os.path.lexists(alias):
            if alias.is_symlink() and os.readlink(alias)==str(runtime/"session.token"):
                alias.unlink()
            else:
                raise SystemExit("Diagnostic token is still active; no routing session was started")
        # The already-running client rereads this path on each IPC reconnect.
        # The target stays protected by its 0600 permissions in a root-owned dir.
        alias.symlink_to(runtime/"session.token")
        alias_inode=alias.lstat().st_ino
    def interrupted(signum,frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGHUP,interrupted)
    signal.signal(signal.SIGTERM,interrupted)
    here=Path(__file__).resolve().parent
    command=["/usr/bin/sudo",sys.executable,str(here/"supervisor.py"),"--uid",str(args.uid),
             "--identity-map",str(args.identity_map.resolve()),"--apply"]
    for target in args.target:command.extend(("--target",target))
    companion=None
    try:
        if args.wine_sockets:
            from bridge import load_identities
            from companion import Companion
            companion=Companion(load_identities(args.identity_map))
            config=companion.start()
            command.extend(("--companion-config",str(config)))
            print("Native WoW socket companion is ready, running as your user.",flush=True)
        result=subprocess.run(command)
        if result.returncode:raise SystemExit(result.returncode)
    except KeyboardInterrupt:
        pass
    finally:
        if companion:
            companion.close()
        if (alias and alias.is_symlink() and alias.lstat().st_ino==alias_inode
                and os.readlink(alias)==str(runtime/"session.token")):
            alias.unlink()
    print("Routing test ended.")


if __name__=="__main__":
    main()
