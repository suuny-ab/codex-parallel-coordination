"""OS blocking serialization between finite controller processes, never lock stealing."""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path


@contextmanager
def instance_lock(path):
    if os.name != 'nt':
        raise RuntimeError('Native shared resource instance currently supports Windows only')
    k = C.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes, k.CreateMutexW.restype = [C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
    k.ReleaseMutex.argtypes, k.ReleaseMutex.restype = [W.HANDLE], W.BOOL
    k.CloseHandle.argtypes = [W.HANDLE]
    name = 'Local\\CoordinationResource-' + hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()
    handle = k.CreateMutexW(None, False, name)
    if not handle:
        raise C.WinError(C.get_last_error())
    owned = False
    try:
        result = k.WaitForSingleObject(handle, 5000)
        owned = result in (0, 0x80)
        if result != 0:
            raise ValueError('resource mutex abandoned/unavailable; preserve and reconcile; no automatic takeover')
        yield
    finally:
        if owned:
            k.ReleaseMutex(handle)
        k.CloseHandle(handle)
