"""Windows DPAPI-protected session cookies; no pickle or passwords on disk."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from desktop.models import parse_profile


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise OSError("Saved sessions require Windows DPAPI.")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class SessionStore:
    def __init__(self, root):
        self.root = Path(root) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, username):
        return self.root / (parse_profile(username) + ".session")

    def accounts(self):
        return sorted(path.stem for path in self.root.glob("*.session"))

    def save(self, username, cookies):
        target = self.path(username)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(protect(json.dumps(cookies).encode("utf-8")))
        os.replace(temporary, target)

    def load(self, username):
        return json.loads(protect(self.path(username).read_bytes(), decrypt=True))

    def delete(self, username):
        self.path(username).unlink(missing_ok=True)
