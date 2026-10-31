"""Finite test-batch mutex shared by this toolkit's current executors."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os

NAME = r"Local\CodexParallelToolkitHeavy-20261031"


@contextmanager
def lease():
    if os.name != "nt":
        raise RuntimeError("Windows resource test batch required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        acquired = result in (0, 0x80)
        if result == 0x80:
            raise RuntimeError("heavy mutex abandoned; reconcile before proceeding")
        if result == 0x102:
            raise RuntimeError("heavy mutex busy; wait for owner completion event")
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
