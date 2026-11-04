"""The desktop, per platform (#192): which program opens a page, shows a notification, sets the volume, controls
the player, switches the output, locks, powers off, reads the clipboard, presses keys or types, on Linux, macOS
and Windows (#193). Callers ask here instead of naming a Linux program, and the Linux commands stay what they were.

On macOS most of it is AppleScript through osascript. Words the user said reach a script only as its arguments
(`item 1 of argv`), never inside the script's text, so nothing said can become a command. Pressing keys and
typing go through System Events, which needs the Accessibility permission (System Settings > Privacy &
Security > Accessibility); switching outputs needs SwitchAudioSource (brew install switchaudio-osx).

On Windows the scripts are Windows PowerShell's (powershell.exe, 5.1, which every Windows 10 and 11 has). The same
rule holds: a script's text is a constant here, and what the user said or a path reaches it only on stdin. Text
goes in and out as UTF-8, so Arabic survives the console's code page.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

OSASCRIPT_SECONDS = 10
MAC_APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications",
                "/System/Applications/Utilities", "~/Applications"]


def system() -> str:
    """"mac", "windows" or "linux": everything else is treated as Linux, as before."""
    if sys.platform == "darwin":
        return "mac"
    return "windows" if sys.platform == "win32" else "linux"


def run(args: list[str], timeout: float = OSASCRIPT_SECONDS, **kwargs) -> subprocess.CompletedProcess:
    """An argument list with a timeout, never a shell; output captured unless the caller says where it goes. On
    Windows text is UTF-8: Python would otherwise read and write it in the ANSI code page, which has no Arabic."""
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs["capture_output"] = True
    if system() == "windows":
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
        kwargs.update(no_window())
    return subprocess.run(args, text=True, timeout=timeout, check=False, **kwargs)


def no_window() -> dict:
    """Popen's options so a console program (PowerShell, icacls, ffplay) opens no window of its own on Windows: Rafiq
    started at logon has no console for it to share."""
    if system() != "windows":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def win_exe(name: str) -> str:
    """A Windows program by its full path in System32 (explorer.exe in the Windows folder itself). A bare name would
    be looked for in the current folder first, so a whoami.exe in a downloaded folder would run instead."""
    root = os.environ.get("SystemRoot") or "C:\\Windows"
    if name == "explorer.exe":
        return os.path.join(root, name)
    if name == "powershell.exe":
        return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", name)
    return os.path.join(root, "System32", name)


def _runner(given: Callable | None) -> Callable:
    """The runner a caller gave, else this module's run (looked up now, so tests can replace it)."""
    return given or globals()["run"]


def _script(lines: list[str]) -> list[str]:
    """osascript with AppleScript lines inside `on run argv`. "--" ends osascript's own options, so an argument
    after it that starts with "-" is still an argument."""
    command = ["osascript"]
    for line in ["on run argv", *lines, "end run"]:
        command += ["-e", line]
    return command + ["--"]


def osascript(lines: list[str], *args: str, run: Callable | None = None,
              timeout: float = OSASCRIPT_SECONDS) -> subprocess.CompletedProcess:
    """Run AppleScript lines, with `args` as argv: the only way words reach a script."""
    return _runner(run)(_script(lines) + list(args), timeout=timeout)


# ---------------------------------------------------------------------- Windows PowerShell

POWERSHELL = ["-NoProfile", "-NonInteractive", "-Command"]
_UTF8 = ("$u = New-Object System.Text.UTF8Encoding $false; "
         "[Console]::InputEncoding = $u; [Console]::OutputEncoding = $u; ")


def powershell(script: str) -> list[str]:
    """A constant script for Windows PowerShell; anything it needs from outside comes on stdin."""
    return [win_exe("powershell.exe"), *POWERSHELL, _UTF8 + script]


def open_command(target: str) -> list[str]:
    """What opens a web address or a link (steam://) with the user's default app."""
    if system() == "windows":
        # ShellExecute through url.dll takes the rest of the line as the target; explorer.exe would split it at
        # commas, as in a maps address (@30.04,31.23,15z), and read "/select," as a switch.
        return [win_exe("rundll32.exe"), "url.dll,FileProtocolHandler", target]
    return ["open", target] if system() == "mac" else ["xdg-open", target]


# A toast from PowerShell's own app id: Windows shows toasts only for an app it knows, and powershell.exe is one.
_TOAST = ("$t = [Console]::In.ReadToEnd(); "
          "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] "
          "| Out-Null; "
          "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
          "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
          "$n = $x.GetElementsByTagName('text'); "
          "$n.Item(0).AppendChild($x.CreateTextNode('Rafiq')) | Out-Null; "
          "$n.Item(1).AppendChild($x.CreateTextNode($t)) | Out-Null; "
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show("
          "[Windows.UI.Notifications.ToastNotification]::new($x))")


def notify(text: str, run: Callable | None = None, which: Callable[[str], str | None] = shutil.which) -> None:
    run = _runner(run)
    if system() == "mac":
        osascript(['display notification (item 1 of argv) with title "Rafiq"'], text[:300], run=run)
    elif system() == "windows":
        try:
            run(powershell(_TOAST), input=text[:300], timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    elif which("notify-send"):
        try:
            run(["notify-send", "Rafiq", text[:300]], timeout=5)
        except (OSError, subprocess.SubprocessError):     # a notification that can't be shown is no failure
            pass


# ---------------------------------------------------------------------- volume (macOS)

class NoVolume(Exception):
    """The output has no volume of its own (HDMI, DisplayPort, some USB): macOS says "missing value"."""


def mac_volume(run: Callable | None = None) -> tuple[int, bool]:
    """The output volume in percent, and whether it is muted."""
    done = osascript(["set s to get volume settings",
                      "return (output volume of s as text) & \",\" & (output muted of s as text)"], run=run)
    level, _, muted = (done.stdout or "").strip().partition(",")
    if not level.strip().isdigit():
        raise NoVolume("this output has no volume control of its own; change it on the device")
    return int(level), muted.strip() == "true"


def set_mac_volume(level: int | None = None, mute: bool | None = None, run: Callable | None = None) -> None:
    if mute is not None:
        osascript(["set volume output muted ((item 1 of argv) is \"true\")"], "true" if mute else "false", run=run)
    if level is not None:
        osascript(["set volume output volume ((item 1 of argv) as integer)"], str(int(level)), run=run)


# ---------------------------------------------------------------------- media (macOS)

# The players AppleScript can drive, and how each says each action. Only these names reach a script's text.
PLAYERS = ("Spotify", "Music")
MAC_MEDIA = {"play": "play", "pause": "pause", "toggle": "playpause", "next": "next track",
             "previous": "previous track", "stop": "pause"}
MEDIA_DONE = {"play": "playing", "pause": "paused", "toggle": "toggled", "next": "skipped to the next on",
              "previous": "went back on", "stop": "stopped"}


def _player_state(player: str, run: Callable | None) -> str:
    """"playing", "paused", "stopped", or "" when the app isn't running. pgrep looks first: a script that names
    an app which isn't installed could make macOS ask "Where is Spotify?"."""
    try:
        running = _runner(run)(["pgrep", "-x", player], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if running.returncode != 0 or not (running.stdout or "").strip():
        return ""
    done = osascript([f'tell application "{player}" to return player state as text'], run=run)
    return (done.stdout or "").strip().lower()


def mac_media(action: str, run: Callable | None = None) -> str:
    """Play, pause, skip or say what plays in Spotify or Music: the one playing, else the one paused."""
    if action not in MAC_MEDIA and action != "status":
        return f"error: action is one of {', '.join([*MAC_MEDIA, 'status'])}"
    states = [(p, _player_state(p, run)) for p in PLAYERS]
    found = next(((p, s) for wanted in ("playing", "paused") for p, s in states if s == wanted), None)
    if found is None:
        return "error: nothing is playing music or video"
    player, state = found
    if action == "status":
        done = osascript([f'tell application "{player}" to return (artist of current track) & " - " & '
                          "(name of current track)"], run=run)
        title = (done.stdout or "").strip().strip(" -")
        return f"{player} is {state}" + (f": {title}" if title else "")
    done = osascript([f'tell application "{player}" to {MAC_MEDIA[action]}'], run=run)
    if done.returncode != 0:
        return f"error: {(done.stderr or '').strip()[:200]}"
    return f"{MEDIA_DONE[action]} {player}"


# ---------------------------------------------------------------------- outputs (macOS)

@dataclass
class Output:
    id: int
    name: str
    default: bool


def mac_outputs(run: Callable | None = None) -> list[Output]:
    run = _runner(run)
    names = [n.strip() for n in (run(["SwitchAudioSource", "-a", "-t", "output"]).stdout or "").splitlines()
             if n.strip()]
    current = (run(["SwitchAudioSource", "-c", "-t", "output"]).stdout or "").strip()
    return [Output(i, name, name == current) for i, name in enumerate(names)]


def set_mac_output(name: str, run: Callable | None = None) -> subprocess.CompletedProcess:
    """The name is one SwitchAudioSource listed, passed as one argument."""
    return _runner(run)(["SwitchAudioSource", "-t", "output", "-s", name])


# ---------------------------------------------------------------------- lock and power

def lock_command() -> list[str]:
    if system() == "windows":
        return [win_exe("rundll32.exe"), "user32.dll,LockWorkStation"]
    if system() == "mac":
        return ["pmset", "displaysleepnow"]        # locks when "require password after sleep" is on, as by default
    session = os.environ.get("XDG_SESSION_ID")
    return ["loginctl", "lock-session"] + ([session] if session else [])


_MAC_POWER = {"shutdown": "shut down", "restart": "restart", "suspend": "sleep"}
_LINUX_POWER = {"shutdown": "poweroff", "restart": "reboot", "suspend": "suspend"}


_WINDOWS_POWER = {"shutdown": ["shutdown.exe", "/s", "/t", "0"], "restart": ["shutdown.exe", "/r", "/t", "0"],
                  # Sleep; Windows hibernates instead when hibernation is on.
                  "suspend": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}


def power_command(action: str) -> list[str]:
    if system() == "windows":
        program, *rest = _WINDOWS_POWER[action]
        return [win_exe(program), *rest]
    if system() == "mac":
        return _script([f'tell application "System Events" to {_MAC_POWER[action]}'])
    return ["systemctl", _LINUX_POWER[action]]


# ---------------------------------------------------------------------- clipboard

def clipboard_commands() -> tuple[list[str], list[str]] | None:
    """(read, write) for this desktop session, or None when there is none."""
    if system() == "windows":
        return (powershell("Get-Clipboard -Raw"),
                powershell("Set-Clipboard -Value ([Console]::In.ReadToEnd())"))
    if system() == "mac":
        return ["pbpaste"], ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-paste", "--no-newline", "--type", "text"], ["wl-copy"]
    if os.environ.get("DISPLAY"):
        return ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]
    return None


# ---------------------------------------------------------------------- keys and typing (macOS)

# The gestures' key presses: macOS key codes (Tab 48, = 24, - 27), and the modifiers; zoom is Command there.
MAC_KEYS = {"next_tab": (48, "control down"), "previous_tab": (48, "control down, shift down"),
            "zoom_in": (24, "command down"), "zoom_out": (27, "command down")}


def key_command(name: str) -> list[str]:
    code, modifiers = MAC_KEYS[name]
    return _script([f'tell application "System Events" to key code {code} using {{{modifiers}}}'])


def type_command() -> list[str]:
    """Type the first argument, as if on the keyboard."""
    return _script(['tell application "System Events" to keystroke (item 1 of argv)'])


def paste_command() -> list[str]:
    # Key code 9 is the V key whatever the layout: keystroke "v" may not be Command-V with an Arabic layout.
    return _script(['tell application "System Events" to key code 9 using {command down}'])


# ---------------------------------------------------------------------- apps (macOS)

def mac_apps(dirs: list[str] | None = None) -> list[dict]:
    """Every .app bundle in the usual folders, as open_app's entries: its name, and `open -a` to start it."""
    found, seen = [], set()
    for folder in dirs or MAC_APP_DIRS:
        for path in sorted(Path(folder).expanduser().glob("*.app")):
            name = path.stem
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append({"id": name, "name": name, "names": [name.lower()], "keywords": [],
                          "launch": ["open", "-a", str(path)]})
    return found


# ---------------------------------------------------------------------- Windows: apps, volume, the settings ACL

def win_app_dirs() -> list[str]:
    """The Start Menu's two folders: this user's, and every user's."""
    tail = os.path.join("Microsoft", "Windows", "Start Menu", "Programs")
    return [os.path.join(os.environ[v], tail) for v in ("APPDATA", "ProgramData") if os.environ.get(v)]


def win_apps(dirs: list[str] | None = None) -> list[dict]:
    """Every shortcut the Start Menu shows, as open_app's entries, each started as a click on it would."""
    found, seen = [], set()
    for folder in win_app_dirs() if dirs is None else dirs:
        for path in sorted(Path(folder).rglob("*.lnk")):
            name = path.stem
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append({"id": name, "name": name, "names": [name.lower()], "keywords": [],
                          "launch": open_command(str(path))})
    return found


def _endpoint():
    """The default output's IAudioEndpointVolume, through pycaw (the `windows` extra)."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities
    except ImportError as exc:
        raise NoVolume('the volume on Windows needs pycaw: pip install -e ".[windows]"') from exc
    try:
        comtypes.CoInitialize()          # once per thread; the voice loop isn't the thread that imported comtypes
    except OSError:
        pass
    speakers = AudioUtilities.GetSpeakers()
    volume = getattr(speakers, "EndpointVolume", None)     # pycaw 20240316 and later
    if volume is None:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume

        volume = cast(speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    return volume


def win_volume() -> tuple[int, bool]:
    endpoint = _endpoint()
    return round(endpoint.GetMasterVolumeLevelScalar() * 100), bool(endpoint.GetMute())


def set_win_volume(level: int | None = None, mute: bool | None = None) -> None:
    endpoint = _endpoint()
    if mute is not None:
        endpoint.SetMute(1 if mute else 0, None)
    if level is not None:
        endpoint.SetMasterVolumeLevelScalar(max(0, min(100, int(level))) / 100, None)


SYSTEM_SID = "S-1-5-18"            # Windows itself, as root is on Linux: a 0700 folder doesn't keep root out either


def user_sid(run: Callable | None = None) -> str | None:
    """This user's SID, which names the account in an ACL whatever the language Windows speaks."""
    import csv

    try:
        done = _runner(run)([win_exe("whoami.exe"), "/user", "/fo", "csv", "/nh"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for row in csv.reader((done.stdout or "").splitlines()):
        if row and row[-1].strip().startswith("S-1-"):
            return row[-1].strip()
    return None


# Groups Windows grants on a drive's root (Everyone, Authenticated Users, Users): an entry for one of them that was
# written on the folder itself, not inherited, would survive /inheritance:r.
OPEN_GROUPS = ("S-1-1-0", "S-1-5-11", "S-1-5-32-545")


def make_private(folder: Path, run: Callable | None = None) -> bool:
    """Only this user (and Windows itself) may open the folder and everything in it, as 0700 and 0600 do on Linux.
    The user becomes the owner of all of it (an owner can always change the permissions back), the inherited
    entries and the open groups' go, and what is inside inherits the folder's. False when it couldn't be done (no
    SID, or a drive with no ACLs such as FAT)."""
    run = _runner(run)
    sid = user_sid(run)
    if not sid:
        return False
    icacls = win_exe("icacls.exe")
    steps = [[icacls, str(folder), "/setowner", f"*{sid}", "/T", "/C", "/Q"],
             [icacls, str(folder), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", f"*{SYSTEM_SID}:(OI)(CI)F",
              "/remove:g", *(f"*{g}" for g in OPEN_GROUPS)],
             [icacls, str(Path(folder) / "*"), "/reset", "/T", "/C", "/Q"]]
    try:
        return all(run(step, timeout=300).returncode == 0 for step in steps)
    except (OSError, subprocess.SubprocessError):
        return False


_ACL = ("$p = [Console]::In.ReadToEnd().Trim(); $a = Get-Acl -LiteralPath $p; "
        "'protected=' + $a.AreAccessRulesProtected; "
        "$o = New-Object Security.Principal.NTAccount $a.Owner; "
        "'owner=' + $o.Translate([Security.Principal.SecurityIdentifier]).Value; "
        "foreach ($r in $a.Access) { $r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }")


def is_private(folder: Path, run: Callable | None = None) -> bool | None:
    """Is the folder this user's, and closed to every account but this user's (and Windows itself)? None when it
    can't be read."""
    run = _runner(run)
    sid = user_sid(run)
    try:
        done = run(powershell(_ACL), input=str(folder), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    if done.returncode != 0 or not sid or len(lines) < 2 or not lines[0].startswith("protected="):
        return None
    return lines[0] == "protected=True" and lines[1] == f"owner={sid}" and set(lines[2:]) <= {sid, SYSTEM_SID}


def lock_exclusive(file) -> None:
    """Hold the file until it closes, against every other Rafiq process: flock, or on Windows a lock on its first
    byte. msvcrt.locking gives up after 10 s; it is asked again, so it waits as long as flock does."""
    if system() == "windows":
        import msvcrt

        file.seek(0)
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue
    import fcntl

    fcntl.flock(file, fcntl.LOCK_EX)
