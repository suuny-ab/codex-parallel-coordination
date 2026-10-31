"""Test-batch agreement only; never a lock for real project computation."""
import ctypes
from ctypes import wintypes


NAME = r'Local\CodexParallelToolkitHeavy-20261031'


class BatchMutex:
    def __init__(self):
        self.handle = None
        self.released = False

    def __enter__(self):
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        api.ReleaseMutex.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.api = api
        handle = api.CreateMutexW(None, False, NAME)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = api.WaitForSingleObject(handle, 0)  # One attempt, no wait/poll/steal.
        if result != 0:
            if result == 0x80:  # WAIT_ABANDONED gives ownership, but is not approval to run.
                api.ReleaseMutex(handle)
            api.CloseHandle(handle)
            raise RuntimeError('Test batch not started: mutex busy/abandoned/failed (%s)' % result)
        self.handle = handle
        return self

    def __exit__(self, *error):
        try:
            if not self.api.ReleaseMutex(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.released = True
        finally:
            self.api.CloseHandle(self.handle)
            self.handle = None
