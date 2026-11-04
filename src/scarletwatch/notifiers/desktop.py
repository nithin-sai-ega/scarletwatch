"""Desktop notification. Only useful when ScarletWatch runs on the machine you are sitting at."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

from .base import Notification, Notifier, NotifyError

_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(10000, $env:SW_TITLE, $env:SW_BODY, 'Info');"
    "Start-Sleep -Seconds 11; $n.Dispose()"
)
_MAC_SCRIPT = (
    "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
)


def build_command(platform: str, title: str, body: str) -> tuple[list[str], dict[str, str]]:
    """Return (argv, extra_env). Text is passed via env/argv, never interpolated into a script."""
    if platform.startswith("win"):
        shell = shutil.which("powershell") or "powershell"
        return (
            [shell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT],
            {"SW_TITLE": title, "SW_BODY": body},
        )
    if platform == "darwin":
        return ["osascript", "-e", _MAC_SCRIPT, title, body], {}
    if shutil.which("notify-send") is None:
        raise NotifyError("notify-send not found (install libnotify-bin) for desktop notifications")
    return ["notify-send", "--urgency=critical", title, body], {}


class DesktopNotifier(Notifier):
    name = "desktop"

    def _run(self, notification: Notification) -> None:
        body = notification.body.replace("\n", " | ")[:250]
        argv, extra = build_command(sys.platform, notification.title, body)
        try:
            if sys.platform.startswith("win"):
                # Fire and forget: the balloon needs the process alive for a few seconds.
                subprocess.Popen(
                    argv,
                    env={**os.environ, **extra},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                subprocess.run(
                    argv,
                    env={**os.environ, **extra},
                    check=True,
                    timeout=10,
                    capture_output=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotifyError(f"desktop notification failed: {type(exc).__name__}") from exc

    async def send(self, notification: Notification) -> None:
        await asyncio.to_thread(self._run, notification)
