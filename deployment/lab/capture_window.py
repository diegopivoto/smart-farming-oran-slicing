#!/usr/bin/env python3
"""Screenshot of one terminal window of a lab PC's display, maximized so its text is legible.

Runs on PC2/PC3 (fed over SSH on stdin, nothing installed): `python3 - "<title substring>"`. Finds the managed window
whose title contains the substring, maximizes and raises it (EWMH client messages through libX11, since the lab PCs
have no wmctrl/xdotool), writes `xwd -id` of it to stdout, then restores its size and the previously focused window.
Exit status 3: no such window. Standard library only.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time

CLIENT_MESSAGE = 33
SUBSTRUCTURE_MASKS = (1 << 19) | (1 << 20)  # SubstructureNotifyMask | SubstructureRedirectMask
REMOVE, ADD = 0, 1
SOURCE_PAGER = 2


class ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class XEvent(ctypes.Union):
    _fields_ = [("xclient", ClientMessage), ("pad", ctypes.c_long * 24)]


x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XInternAtom.restype = ctypes.c_ulong
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(XEvent)]
x11.XFlush.argtypes = [ctypes.c_void_p]


def send(display: int, window: int, message: str, *data: int) -> None:
    atom = x11.XInternAtom(display, message.encode(), 0)
    event = XEvent()
    event.xclient = ClientMessage(type=CLIENT_MESSAGE, send_event=1, display=display, window=window,
                                  message_type=atom, format=32)
    for i, value in enumerate(data):
        event.xclient.data[i] = value
    x11.XSendEvent(display, x11.XDefaultRootWindow(display), 0, SUBSTRUCTURE_MASKS, ctypes.byref(event))
    x11.XFlush(display)


def maximize(display: int, window: int, on: bool) -> None:
    horz = x11.XInternAtom(display, b"_NET_WM_STATE_MAXIMIZED_HORZ", 0)
    vert = x11.XInternAtom(display, b"_NET_WM_STATE_MAXIMIZED_VERT", 0)
    send(display, window, "_NET_WM_STATE", ADD if on else REMOVE, horz, vert, SOURCE_PAGER)


def windows() -> list[tuple[int, str]]:
    ids = re.findall(r"0x[0-9a-f]+", subprocess.run(("xprop", "-root", "_NET_CLIENT_LIST"),
                                                    capture_output=True, text=True, check=True).stdout)
    found = []
    for wid in ids:
        name = subprocess.run(("xprop", "-id", wid, "_NET_WM_NAME"), capture_output=True, text=True).stdout
        found.append((int(wid, 16), name.partition("=")[2].strip().strip('"')))
    return found


def main() -> int:
    title = sys.argv[1]
    found = windows()
    match = next((wid for wid, name in found if name == title), None)  # exact title first, then substring
    match = match or next((wid for wid, name in found if title in name), None)
    if match is None:
        return 3
    active = re.findall(r"0x[0-9a-f]+", subprocess.run(("xprop", "-root", "_NET_ACTIVE_WINDOW"),
                                                       capture_output=True, text=True).stdout)
    display = x11.XOpenDisplay(None)
    maximize(display, match, True)
    send(display, match, "_NET_ACTIVE_WINDOW", SOURCE_PAGER, 0, 0)
    time.sleep(1.5)  # let the window manager resize and the terminal redraw
    shot = subprocess.run(("xwd", "-id", hex(match), "-silent"), capture_output=True)
    maximize(display, match, False)
    if active and int(active[0], 16):
        send(display, int(active[0], 16), "_NET_ACTIVE_WINDOW", SOURCE_PAGER, 0, 0)
    sys.stdout.buffer.write(shot.stdout)
    return shot.returncode


if __name__ == "__main__":
    raise SystemExit(main())
