#!/usr/bin/python3
"""Desktop setup and launcher; the vendor client always runs as the user.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import atexit
import fcntl
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'installers'))
from bridge_setup.core import Setup, default_state, find_client
from bridge_setup.util import State, private_json, read_private


def starttime(pid):
    return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]


class Desktop:
    def __init__(self, manage=False):
        if os.geteuid() == 0:
            raise SystemExit('Open ExitLag Bridge from your desktop as the game user')
        self.state = default_state()
        self.window = tk.Tk()
        self.window.title('ExitLag Bridge')
        self.window.geometry('570x400')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        signal.signal(signal.SIGTERM, lambda *_: self.window.after(0, self.close))
        self.events = queue.Queue()
        self.busy = False
        self.client = self.helper = None
        self.lock = None
        self.started = None
        self.last_check = 0
        self.checking = False
        self.message = tk.StringVar(value='Download the official Windows installer, then choose its .exe.')
        box = ttk.Frame(self.window, padding=24); box.pack(fill='both', expand=True)
        ttk.Label(box, text='ExitLag Bridge', font=('', 20, 'bold')).pack(anchor='w')
        ttk.Label(box, text='Unofficial Linux compatibility launcher').pack(anchor='w', pady=(0, 16))
        ttk.Label(box, textvariable=self.message, wraplength=510).pack(anchor='w', pady=8)
        ttk.Label(box, text='Review the vendor terms in its wizard. Leave Launch ExitLag unchecked.\nYour account stays in your own private installation.', wraplength=510).pack(anchor='w', pady=8)
        ttk.Label(box, text='If the wizard warns that its Windows NDIS driver could not install, click OK to continue. The bridge supplies native routing instead.', wraplength=510).pack(anchor='w', pady=8)
        buttons = ttk.Frame(box); buttons.pack(anchor='w', pady=10)
        ttk.Button(buttons, text='Download from ExitLag', command=lambda: webbrowser.open('https://www.exitlag.com/download')).pack(side='left', padx=(0, 8))
        self.choose = ttk.Button(buttons, text='Choose installer…', command=self.install); self.choose.pack(side='left')
        bottom = ttk.Frame(box); bottom.pack(anchor='w', pady=8)
        ttk.Button(bottom, text='Configure games…', command=self.games).pack(side='left', padx=(0, 8))
        ttk.Button(bottom, text='Launch ExitLag', command=self.launch).pack(side='left', padx=(0, 8))
        ttk.Button(bottom, text='Quit', command=self.close).pack(side='left')
        self.window.after(300, self.poll)
        if manage:
            self.window.after(100, self.games)
        elif self.configured():
            self.window.after(100, self.launch)

    def configured(self):
        try:
            return State(self.state).load().get('complete') and find_client(self.state / 'prefix') is not None
        except (OSError, ValueError):
            return False

    def install(self):
        if self.busy or self.client:
            return
        selected = filedialog.askopenfilename(title='Official ExitLag Windows installer', filetypes=[('Windows executable', '*.exe')])
        if not selected:
            return
        self.busy = True; self.choose.state(['disabled'])
        def work():
            try:
                Setup(self.state, ROOT / 'installers/payloads', emit=lambda stage, message: self.events.put(message)).run(Path(selected))
            except Exception as error:
                self.events.put('Setup stopped: ' + str(error))
            finally:
                self.events.put(None)
        threading.Thread(target=work, daemon=True).start()

    def games(self):
        if self.busy or self.helper:
            return
        try:
            State(self.state, create=True)
        except (OSError, ValueError) as error:
            messagebox.showerror('Setup directory', str(error)); return
        dialog = tk.Toplevel(self.window); dialog.title('Game mappings'); dialog.geometry('680x400')
        ttk.Label(dialog, text='One mapping per line: game path, a Tab, then its Windows executable path.\nUse the executable that owns the game connection. A Wine-prefix mapping covers that whole prefix.', wraplength=640).pack(padx=16, pady=12, anchor='w')
        editor = tk.Text(dialog, wrap='none', height=12); editor.pack(padx=16, fill='both', expand=True)
        path = self.state / 'games.map'
        if path.exists():
            editor.insert('1.0', path.read_text())
        def add(prefix=False):
            selected = filedialog.askdirectory(title='Game Wine prefix') if prefix else filedialog.askopenfilename(title='Game executable')
            if selected:
                identity = simpledialog.askstring('Windows game identity', 'Windows executable path recognized by ExitLag (for example C:\\Games\\World of Warcraft\\_classic_era_\\WowClassic.exe):', parent=dialog)
                if identity:
                    source = ('wineprefix:' if prefix else '') + str(Path(selected).resolve())
                    editor.insert('end', source + '\t' + identity + '\n')
        def save():
            text = editor.get('1.0', 'end').strip() + '\n'
            # Same parser used by the installed supervisor, running unprivileged here.
            sys.path.insert(0, str(Path(__file__).parent))
            from run_root import read_map
            temporary = self.state / 'games.draft.map'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as output: output.write(text)
            try:
                read_map(temporary, os.getuid())
                os.replace(temporary, path)
                dialog.destroy()
            except Exception as error:
                messagebox.showerror('Game mappings', str(error), parent=dialog)
                temporary.unlink(missing_ok=True)
        controls = ttk.Frame(dialog); controls.pack(padx=16, pady=12, anchor='w')
        for label, command in [('Add executable…', lambda: add()), ('Add Wine prefix…', lambda: add(True)), ('Save', save)]:
            ttk.Button(controls, text=label, command=command).pack(side='left', padx=(0, 8))

    def launch(self):
        if self.busy or self.helper:
            return
        if not self.configured():
            self.message.set('Complete setup using the official installer first.'); return
        if not (self.state / 'games.map').is_file():
            self.message.set('Configure the game you want to route first.'); self.games(); return
        self.lock = os.open(self.state / 'desktop.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock); self.lock = None
            self.message.set('ExitLag Bridge is already running.'); return
        private_json(self.state / 'desktop-session.json', dict(pid=os.getpid(), start=starttime(os.getpid())))
        leasefd = os.open(self.state / 'desktop.lease', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(leasefd, 'w') as output: output.write('run\n')
        self.started = time.time()
        self.helper = subprocess.Popen(['pkexec', '/usr/bin/python3', '-I', str(ROOT / 'installers/linux/run_root.py'), '--uid', str(os.getuid()), '--parent', str(os.getpid()), '--map', str(self.state / 'games.map'), '--lease', str(self.state / 'desktop.lease')], stdin=subprocess.DEVNULL)
        self.message.set('Authorize routing in the system dialog. ExitLag will open when the helper is ready.')

    def poll(self):
        try:
            while True:
                value = self.events.get_nowait()
                if value == ('client_closed',):
                    self.stop_session()
                    self.window.destroy()
                    return
                elif value is None:
                    self.busy = False; self.choose.state(['!disabled'])
                else: self.message.set(value)
        except queue.Empty:
            pass
        if self.helper:
            (self.state / 'desktop.lease').touch()
            if self.helper.poll() is not None:
                self.message.set('Routing stopped or authorization was cancelled. You can retry Launch ExitLag.')
                self.stop_session()
                self.window.deiconify()
            elif not self.client:
                token = Path('/run/exitlag-linux/ipc.token')
                if token.exists() and token.stat().st_uid == os.getuid() and token.stat().st_mtime >= self.started:
                    client = find_client(self.state / 'prefix')
                    env = os.environ.copy()
                    for name in list(env):
                        if name.startswith('EXITLAG_') or name in ('WINEDLLPATH', 'WINELOADER', 'QT_QUICK_BACKEND', 'QT_OPENGL'):
                            env.pop(name, None)
                    env.update(WINEPREFIX=str(self.state / 'prefix'), WINEDEBUG='-all', QSG_RHI_BACKEND='opengl', EXITLAG_NETD='127.0.0.1:51900', EXITLAG_NETD_TOKEN='Z:' + str(token))
                    logfd = os.open(self.state / 'client.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(logfd, 'ab') as log:
                        self.client = subprocess.Popen([str(self.state / 'runtime/bin/wine'), str(client)], cwd=client.parent, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                    self.message.set('Routing is active. Select your game and apply its routes in ExitLag.')
                    self.window.withdraw()
                elif time.time() - self.started > 120:
                    self.message.set('The routing helper did not start. Close and try again.')
                    self.stop_session()
            elif time.time() - self.started > 25 and time.time() - self.last_check > 5 and not self.checking:
                self.last_check = time.time(); self.checking = True
                def check_client():
                    try:
                        env = os.environ.copy(); env.update(WINEPREFIX=str(self.state / 'prefix'), WINEDEBUG='-all')
                        result = subprocess.run([str(self.state / 'runtime/bin/wine'), 'tasklist.exe', '/FI', 'IMAGENAME eq ExitLag.exe', '/NH'], env=env, capture_output=True, timeout=10)
                        if result.returncode == 0 and b'exitlag.exe' not in result.stdout.lower():
                            self.events.put(('client_closed',))
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    finally: self.checking = False
                threading.Thread(target=check_client, daemon=True).start()
        self.window.after(500, self.poll)

    def stop_session(self):
        (self.state / 'desktop.lease').unlink(missing_ok=True)
        if self.client:
            env = os.environ.copy(); env.update(WINEPREFIX=str(self.state / 'prefix'), WINEDEBUG='-all')
            subprocess.run([str(self.state / 'runtime/bin/wine'), 'taskkill.exe', '/IM', 'ExitLag.exe', '/F'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            self.client = None
        if self.helper and self.helper.poll() is None:
            # The elevated supervisor watches this process; it stops after exit.
            pass
        self.helper = None
        if self.lock is not None:
            os.close(self.lock); self.lock = None

    def close(self):
        if self.busy:
            messagebox.showinfo('Setup in progress', 'Finish or cancel the vendor installer before closing.'); return
        self.stop_session()
        self.window.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manage-games', action='store_true')
    parser.add_argument('--quit', action='store_true')
    args = parser.parse_args()
    if args.quit:
        try:
            record = read_private(default_state() / 'desktop-session.json')
            pid = record['pid']
            if (Path('/proc') / str(pid)).stat().st_uid == os.getuid() and starttime(pid) == record['start']:
                os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass
        return
    Desktop(args.manage_games).window.mainloop()


if __name__ == '__main__':
    main()
