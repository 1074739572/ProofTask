"""Read/write the host OS clipboard for TUI paste and copy (Windows-first).

Textual's ``App.clipboard`` / ``copy_to_clipboard`` only mirrors in-app text (or
OSC 52). On Windows Terminal, Ctrl+V / Ctrl+C therefore often do nothing useful
unless we talk to the real system clipboard.

Critical Windows detail: ``GetClipboardData`` / ``SetClipboardData`` must use a
pointer-sized handle type. The ctypes default (``c_int``) truncates 64-bit
handles, which made our first Win32 reader silently return empty while tkinter
still worked — and tkinter is unreliable once the Textual event loop is running.

Copy path must also avoid OSC 52 after a successful Win32 write: Windows
Terminal can fight or overwrite the OS clipboard when both update at once.
"""

from __future__ import annotations

import subprocess
import sys


def read_os_clipboard() -> str:
    """Best-effort Unicode text from the OS clipboard; empty string on failure."""
    if sys.platform == "win32":
        text = _windows_clipboard()
        if text:
            return text
        text = _powershell_clipboard()
        if text:
            return text
    text = _tk_clipboard()
    if text:
        return text
    return ""


def write_os_clipboard(text: str) -> bool:
    """Write Unicode text to the OS clipboard. Returns True on success."""
    payload = text if text is not None else ""
    if sys.platform == "win32":
        if _windows_set_clipboard(payload):
            return True
        if _powershell_set_clipboard(payload):
            return True
    return _tk_set_clipboard(payload)


def _windows_set_clipboard(text: str) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def _proto(fn, *, argtypes=None, restype=None) -> None:
        try:
            if argtypes is not None:
                fn.argtypes = argtypes
            if restype is not None:
                fn.restype = restype
        except (AttributeError, TypeError, ValueError):
            pass

    _proto(user32.OpenClipboard, argtypes=[wintypes.HWND], restype=wintypes.BOOL)
    _proto(user32.CloseClipboard, argtypes=[], restype=wintypes.BOOL)
    _proto(user32.EmptyClipboard, argtypes=[], restype=wintypes.BOOL)
    _proto(user32.SetClipboardData, argtypes=[wintypes.UINT, ctypes.c_void_p], restype=ctypes.c_void_p)
    _proto(kernel32.GlobalAlloc, argtypes=[wintypes.UINT, ctypes.c_size_t], restype=ctypes.c_void_p)
    _proto(kernel32.GlobalLock, argtypes=[ctypes.c_void_p], restype=ctypes.c_void_p)
    _proto(kernel32.GlobalUnlock, argtypes=[ctypes.c_void_p], restype=wintypes.BOOL)
    _proto(kernel32.GlobalFree, argtypes=[ctypes.c_void_p], restype=ctypes.c_void_p)

    # CF_UNICODETEXT wants UTF-16LE with trailing NUL.
    data = (text.replace("\n", "\r\n") if "\r" not in text else text).encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        # Ownership transferred to the system on success — do not GlobalFree.
        return True
    except Exception:
        try:
            kernel32.GlobalFree(handle)
        except Exception:
            pass
        return False
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def _powershell_set_clipboard(text: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Set-Clipboard -Value $input",
            ],
            check=False,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except Exception:
        return False
    return completed.returncode == 0


def _tk_set_clipboard(text: str) -> bool:
    try:
        import tkinter as tk
    except Exception:
        return False
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        return True
    except Exception:
        return False
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _windows_clipboard() -> str:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return ""

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    CF_TEXT = 1

    def _proto(fn, *, argtypes=None, restype=None) -> None:
        try:
            if argtypes is not None:
                fn.argtypes = argtypes
            if restype is not None:
                fn.restype = restype
        except (AttributeError, TypeError, ValueError):
            pass

    # 64-bit safe prototypes — without these, handles truncate to 32-bit.
    _proto(user32.OpenClipboard, argtypes=[wintypes.HWND], restype=wintypes.BOOL)
    _proto(user32.CloseClipboard, argtypes=[], restype=wintypes.BOOL)
    _proto(
        user32.IsClipboardFormatAvailable,
        argtypes=[wintypes.UINT],
        restype=wintypes.BOOL,
    )
    _proto(user32.GetClipboardData, argtypes=[wintypes.UINT], restype=ctypes.c_void_p)
    _proto(kernel32.GlobalLock, argtypes=[ctypes.c_void_p], restype=ctypes.c_void_p)
    _proto(kernel32.GlobalUnlock, argtypes=[ctypes.c_void_p], restype=wintypes.BOOL)

    if not user32.OpenClipboard(None):
        return ""
    try:
        for fmt, decoder in (
            (CF_UNICODETEXT, lambda p: ctypes.wstring_at(p)),
            (CF_TEXT, lambda p: ctypes.string_at(p).decode("utf-8", errors="replace")),
        ):
            if not user32.IsClipboardFormatAvailable(fmt):
                continue
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                text = decoder(pointer) or ""
            finally:
                kernel32.GlobalUnlock(handle)
            if text:
                return text.replace("\r\n", "\n").replace("\r", "\n")
        return ""
    except Exception:
        return ""
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def _powershell_clipboard() -> str:
    """Fallback when Win32 OpenClipboard is briefly locked by another process."""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; Get-Clipboard -Raw",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    text = completed.stdout or ""
    # PowerShell may append a trailing newline for -Raw on some hosts.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _tk_clipboard() -> str:
    try:
        import tkinter as tk
    except Exception:
        return ""
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        text = root.clipboard_get() or ""
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        return ""
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
