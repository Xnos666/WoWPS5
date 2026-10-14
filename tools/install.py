#!/usr/bin/env python3
"""Install WoW for PS5 on a console, and copy your own game to it.

  python3 install.py --host 192.168.1.50 --wow "/path/to/World of Warcraft"

The app runs WoW Forever (_classic_beta_) and WoW Classic Era (_classic_era_).
Every one installed on the PC is copied (--flavor picks), and the app starts
the first it finds on the console, Forever before Classic Era; --start picks
another (it writes the WOWPS5_GAME line of /data/wowps5/launch.txt).

Run it from the unpacked release bundle (the folder that holds title/ and
runtime/), or name that folder with --bundle. It uploads over the console's
FTP server:

  title/PPSA99220      ->  /data/homebrew/PPSA99220      the app (ShadowMount shows it on the home screen)
  runtime/wowps5       ->  /data/wowps5                  Wine's Windows side and an empty Windows prefix
                                                         (prefix/drive_c goes to prefix/dosdevices/c:)
  your game            ->  /data/wowps5/prefix/dosdevices/c:/Games/World of Warcraft   (about 64 GiB)

Your installation is only read. A file already on the console with the same
size is skipped, so an interrupted run is continued by running it again.
Nothing of an account is copied: you sign in on the console.

After the game has updated on the PC, run it again the same way (--skip-app
copies only the game). An update changes files without changing their size, so
what was sent is remembered in game-sent.json beside this script, and a file
that has changed on the PC since then is sent again. If the game was copied by
hand or by an earlier version, there is nothing remembered yet: add
--since "YYYY-MM-DD HH:MM", the time of that copy, to send what changed after it.

A game's WTF folder is not copied (PC settings do not suit the console), but a
game with no WTF/Config.wtf on the console gets a short one with its region and
language: without them the client asks in a window the console cannot show,
and the screen stays black.
"""
import argparse
import datetime
import io
import json
import sys
import time
from ftplib import FTP, all_errors, error_perm
from pathlib import Path

GAME_TARGET = 'prefix/dosdevices/c:/Games/World of Warcraft'
SKIP_TOP = {'_classic_', '_retail_', '.battle.net', 'World of Warcraft Launcher.exe'}
# The game folders this port can run, with their programs. --flavor picks which are copied.
FLAVORS = {'_classic_beta_': 'WowB.exe', '_classic_era_': 'WowClassic.exe'}
CHOSEN = set(FLAVORS)
SKIP_FLAVOR = {'WTF', 'Interface', 'Logs', 'Errors', 'Cache', 'Screenshots',
               'vkd3d-proton.cache', 'vkd3d-proton.cache.write'}
# A first WTF/Config.wtf for a game on the console: these settings of the PC's own
# Config.wtf when it has them, else these defaults; GxApi is always D3D12, the
# only graphics the port translates.
SEED_KEYS = ('portal', 'agentUID', 'textLocale', 'audioLocale')
SEED_DEFAULTS = {
    '_classic_beta_': {'portal': 'test', 'agentUID': 'wow_classic_beta', 'textLocale': 'enUS', 'audioLocale': 'enUS'},
    '_classic_era_': {'portal': 'US', 'agentUID': 'wow_classic_era', 'textLocale': 'enUS', 'audioLocale': 'enUS'},
}


class Console:
    def __init__(self, args):
        self.args = args
        self.made = set()
        self.connect()

    def connect(self):
        self.ftp = FTP()
        self.ftp.connect(self.args.host, self.args.port, timeout=600)
        self.ftp.login(self.args.user, self.args.password)
        self.ftp.voidcmd('TYPE I')

    def size(self, path):
        try:
            return self.ftp.size(path)
        except all_errors:
            return None

    def get(self, remote):
        """The file's bytes, or None when the console has no such file."""
        stream = io.BytesIO()
        try:
            self.ftp.retrbinary('RETR ' + remote, stream.write)
        except error_perm:
            return None
        return stream.getvalue()

    def put(self, data, remote):
        self.ensure(remote.rsplit('/', 1)[0])
        self.ftp.storbinary('STOR ' + remote, io.BytesIO(data))

    def ensure(self, directory):
        path = ''
        for part in directory.strip('/').split('/'):
            path += '/' + part
            if path not in self.made:
                try:
                    self.ftp.mkd(path)
                except error_perm:
                    pass
                self.made.add(path)

    def send(self, local, remote, stale=False):
        """True when sent, False when it was there already. stale: send it though the size matches."""
        size = local.stat().st_size
        # The console's FTP servers answer for an executable with another size than
        # the file's (they read it as the system would): those are sent every time.
        signed = local.name == 'eboot.bin' or local.suffix in ('.prx', '.sprx', '.self', '.elf')
        if not signed and not stale and self.size(remote) == size:
            return False
        self.ensure(remote.rsplit('/', 1)[0])
        problem = 'the size on the console does not match'
        for attempt in (1, 2, 3):
            try:
                with local.open('rb') as stream:
                    self.ftp.storbinary('STOR ' + remote, stream, blocksize=1024 * 1024)
                if signed or self.size(remote) == size:
                    return True
            except all_errors as error:
                problem = str(error) or type(error).__name__
            time.sleep(2)
            try:
                self.connect()
            except all_errors as error:
                problem = f'the console stopped answering ({error})'
        sys.exit(f'could not send {local.name} to {remote}: {problem}\n'
                 'Is the console still on, with free space? If the app is running on the console, close it first.')


def upload_tree(console, source, target, what):
    files = sorted(p for p in source.rglob('*') if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f'{what}: {len(files)} files, {total / 2**20:.0f} MiB, to {target}', flush=True)
    sent = done = 0
    began = time.time()
    for path in files:
        relative = path.relative_to(source).as_posix()
        if relative.startswith('prefix/drive_c/'):       # the console's name for it, which a Windows PC cannot hold
            relative = 'prefix/dosdevices/c:/' + relative[len('prefix/drive_c/'):]
        sent += console.send(path, f'{target}/{relative}')
        done += path.stat().st_size
        if time.time() - began > 15:
            began = time.time()
            print(f'  {done * 100 // max(total, 1)}%', flush=True)
    print(f'  done: {sent} sent, {len(files) - sent} already there', flush=True)


def game_files(install):
    for path in sorted(install.rglob('*')):
        relative = path.relative_to(install)
        if relative.parts[0] in SKIP_TOP:
            continue
        if relative.parts[0] in FLAVORS and relative.parts[0] not in CHOSEN:
            continue
        if relative.parts[0] in FLAVORS and len(relative.parts) > 1 and relative.parts[1] in SKIP_FLAVOR:
            continue
        if path.is_file() and not path.is_symlink():
            yield relative, path


def upload_game(console, install, target, since=None):
    if not any((install / d / e).is_file() for d, e in FLAVORS.items() if d in CHOSEN):
        sys.exit(f'{install} holds none of {", ".join(f"{d}/{e}" for d, e in FLAVORS.items() if d in CHOSEN)}: '
                 '--wow is the folder with Data and the game folder (_classic_beta_ or _classic_era_) in it')
    files = list(game_files(install))
    total = sum(path.stat().st_size for _, path in files)
    print(f'the game: {len(files)} files, {total / 2**30:.1f} GiB, to {target}', flush=True)
    record_file = Path(__file__).resolve().parent / 'game-sent.json'
    record = json.loads(record_file.read_text()) if record_file.is_file() else {}
    sent_bytes = sent = 0
    started = time.time()
    for relative, path in files:
        now = [path.stat().st_size, int(path.stat().st_mtime)]
        known = record.get(relative.as_posix())
        stale = known != now if known else (since is not None and now[1] > since)
        was_sent = console.send(path, f'{target}/{relative.as_posix()}', stale)
        record[relative.as_posix()] = now
        if was_sent:
            record_file.write_text(json.dumps(record))
            sent += 1
            sent_bytes += path.stat().st_size
            if path.stat().st_size >= 64 << 20:
                minutes = (time.time() - started) / 60
                print(f'  {relative.as_posix()}: {sent_bytes / 2**30:.1f} GiB sent in {minutes:.0f} min', flush=True)
    record_file.write_text(json.dumps(record))
    print(f'  done: {sent} sent ({sent_bytes / 2**30:.1f} GiB), {len(files) - sent} already there', flush=True)


def seed_config(console, install, target):
    """A WTF/Config.wtf for each game copied that has none on the console yet."""
    for folder, program in FLAVORS.items():
        if folder not in CHOSEN or not (install / folder / program).is_file():
            continue
        remote = f'{target}/{folder}/WTF/Config.wtf'
        if console.size(remote) is not None:
            continue
        values = dict(SEED_DEFAULTS[folder])
        pc = install / folder / 'WTF' / 'Config.wtf'
        if pc.is_file():
            for line in pc.read_text(encoding='utf-8', errors='replace').splitlines():
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[0].upper() == 'SET':
                    for key in SEED_KEYS:
                        if parts[1].lower() == key.lower():
                            values[key] = parts[2].strip().strip('"')
        values['GxApi'] = 'D3D12'
        console.put(''.join(f'SET {key} "{value}"\n' for key, value in values.items()).encode(), remote)
        print(f'{folder}/WTF/Config.wtf written on the console: {", ".join(f"{k} {v}" for k, v in values.items())}', flush=True)


def choose_game(console, root, folder):
    """Which game the app starts: the WOWPS5_GAME line of launch.txt (auto: no line, the first installed)."""
    settings = f'{root}/launch.txt'
    note = '# install.py --start: the game the app starts'
    lines = [line for line in (console.get(settings) or b'').decode('utf-8', 'replace').splitlines()
             if not line.startswith('WOWPS5_GAME=') and line != note]
    if folder != 'auto':
        lines += [note, f'WOWPS5_GAME={root}/{GAME_TARGET}/{folder}/{FLAVORS[folder]}']
        if folder == '_classic_era_' and '+-d3d12' not in lines:
            lines.append('+-d3d12')   # keeps Classic Era on DirectX 12, the only graphics the port translates
    console.put(''.join(line + '\n' for line in lines).encode(), settings)
    print(f'{settings}: the app starts ' + ('the first game installed' if folder == 'auto' else FLAVORS[folder]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', required=True, help="the console's address on your network")
    parser.add_argument('--port', type=int, default=2121, help="the console's FTP port (default 2121)")
    parser.add_argument('--user', default='anonymous')
    parser.add_argument('--password', default='')
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent,
                        help='the unpacked release bundle (default: where this script is)')
    parser.add_argument('--wow', type=Path, help='your World of Warcraft folder (the one with Data and _classic_beta_ or _classic_era_)')
    parser.add_argument('--skip-app', action='store_true', help='only copy the game')
    parser.add_argument('--flavor', choices=sorted(FLAVORS), action='append',
                        help='copy only this game folder (repeatable; default: every one installed). '
                             'With only _classic_era_ on the console, the app starts Classic Era')
    parser.add_argument('--start', choices=['auto', *sorted(FLAVORS)],
                        help='the game the app starts (auto: the first on the console, Forever before Classic Era); '
                             'works with --skip-app and no --wow')
    parser.add_argument('--since', help='after a game update, with no record of an earlier copy: also send game files '
                                        'changed on the PC after this time ("YYYY-MM-DD HH:MM")')
    parser.add_argument('--title-dir', default='/data/homebrew/PPSA99220', help=argparse.SUPPRESS)
    parser.add_argument('--root', default='/data/wowps5', help=argparse.SUPPRESS)
    args = parser.parse_args()
    global CHOSEN
    CHOSEN = set(args.flavor or FLAVORS)
    since = datetime.datetime.strptime(args.since, '%Y-%m-%d %H:%M').timestamp() if args.since else None
    title, runtime = args.bundle / 'title' / 'PPSA99220', args.bundle / 'runtime' / 'wowps5'
    if not args.skip_app and not ((title / 'eboot.bin').is_file() and runtime.is_dir()):
        sys.exit(f'{args.bundle} is not an unpacked release bundle (no title/PPSA99220/eboot.bin and runtime/wowps5)')
    try:
        console = Console(args)
    except all_errors as error:
        sys.exit(f'no FTP server at {args.host}:{args.port} ({error}): is the console on, jailbroken, and its FTP server running?')
    if not args.skip_app:
        upload_tree(console, title, args.title_dir, 'the app')
        upload_tree(console, runtime, args.root, 'Wine and the Windows prefix')
    if args.wow:
        upload_game(console, args.wow, f'{args.root}/{GAME_TARGET}', since)
        seed_config(console, args.wow, f'{args.root}/{GAME_TARGET}')
    else:
        print('no --wow given: the game was not copied')
    if args.start:
        choose_game(console, args.root, args.start)
    print('finished. On the console: "WoW Forever" on the home screen (ShadowMount adds it within a minute).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
