"""Windows-owned Job Object lifecycle, event-driven; never finds/kills by name."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess

if os.name != 'nt':
    raise RuntimeError('Resource process control currently supports Windows only')
k32 = C.WinDLL('kernel32', use_last_error=True)
psapi = C.WinDLL('psapi', use_last_error=True)
SIZE = C.c_size_t


def api(lib, name, result, *args):
    f = getattr(lib, name)
    f.restype, f.argtypes = result, args
    return f


def ok(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


class Performance(C.Structure):
    _fields_ = [('cb', W.DWORD)] + [(x, SIZE) for x in ('CommitTotal', 'CommitLimit', 'CommitPeak',
        'PhysicalTotal', 'PhysicalAvailable', 'SystemCache', 'KernelTotal', 'KernelPaged', 'KernelNonpaged', 'PageSize')] + \
        [(x, W.DWORD) for x in ('HandleCount', 'ProcessCount', 'ThreadCount')]


class Memory(C.Structure):
    _fields_ = [('cb', W.DWORD), ('PageFaultCount', W.DWORD)] + [(x, SIZE) for x in
        ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
         'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]


class Startup(C.Structure):
    _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR)] + \
        [(x, W.DWORD) for x in ('dwX', 'dwY', 'dwXSize', 'dwYSize', 'dwXCountChars', 'dwYCountChars', 'dwFillAttribute', 'dwFlags')] + \
        [('wShowWindow', W.WORD), ('cbReserved2', W.WORD), ('lpReserved2', C.c_void_p)] + \
        [(x, W.HANDLE) for x in ('hStdInput', 'hStdOutput', 'hStdError')]


class Process(C.Structure):
    _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE), ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]


class Port(C.Structure):
    _fields_ = [('CompletionKey', C.c_void_p), ('CompletionPort', W.HANDLE)]


close = api(k32, 'CloseHandle', W.BOOL, W.HANDLE)
open_process = api(k32, 'OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
get_times = api(k32, 'GetProcessTimes', W.BOOL, W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME))
get_exit = api(k32, 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
wait_process = api(k32, 'WaitForSingleObject', W.DWORD, W.HANDLE, W.DWORD)
query_job = api(k32, 'QueryInformationJobObject', W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
set_job = api(k32, 'SetInformationJobObject', W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
open_job = api(k32, 'OpenJobObjectW', W.HANDLE, W.DWORD, W.BOOL, W.LPCWSTR)
create_job = api(k32, 'CreateJobObjectW', W.HANDLE, C.c_void_p, W.LPCWSTR)
create_port = api(k32, 'CreateIoCompletionPort', W.HANDLE, W.HANDLE, W.HANDLE, SIZE, W.DWORD)
get_packet = api(k32, 'GetQueuedCompletionStatus', W.BOOL, W.HANDLE, C.POINTER(W.DWORD), C.POINTER(SIZE), C.POINTER(C.c_void_p), W.DWORD)
create_process = api(k32, 'CreateProcessW', W.BOOL, W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(Startup), C.POINTER(Process))
assign_job = api(k32, 'AssignProcessToJobObject', W.BOOL, W.HANDLE, W.HANDLE)
resume = api(k32, 'ResumeThread', W.DWORD, W.HANDLE)
terminate_job = api(k32, 'TerminateJobObject', W.BOOL, W.HANDLE, W.UINT)
is_in_job = api(k32, 'IsProcessInJob', W.BOOL, W.HANDLE, W.HANDLE, C.POINTER(W.BOOL))


def system_memory():
    value = Performance(cb=C.sizeof(Performance))
    ok(api(psapi, 'GetPerformanceInfo', W.BOOL, C.POINTER(Performance), W.DWORD)(C.byref(value), value.cb))
    return {'source': 'Windows GetPerformanceInfo', 'injected': False,
        'physical_available': value.PhysicalAvailable * value.PageSize,
        'commit_available': (value.CommitLimit - value.CommitTotal) * value.PageSize,
        'physical_total': value.PhysicalTotal * value.PageSize,
        'commit_limit': value.CommitLimit * value.PageSize}


def identity(handle, pid):
    created, exited, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
    ok(get_times(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)))
    code = W.DWORD()
    ok(get_exit(handle, C.byref(code)))
    waited = wait_process(handle, 0)
    if waited not in (0, 0x102):
        raise C.WinError(C.get_last_error())
    return {'pid': int(pid), 'created': (created.dwHighDateTime << 32) | created.dwLowDateTime,
            'exit_code': int(code.value), 'running': waited == 0x102}


def pid_identity(pid, job=None, measure=False):
    handle = ok(open_process(0x1000 | 0x100000 | (0x0400 | 0x0010 if measure else 0), False, pid))
    try:
        if job is not None:
            member = W.BOOL()
            ok(is_in_job(handle, job, C.byref(member)))
            if not member.value:
                raise OSError('PID left the registered Job during observation')
        value = identity(handle, pid)
        if measure:
            memory = Memory(cb=C.sizeof(Memory))
            ok(api(psapi, 'GetProcessMemoryInfo', W.BOOL, W.HANDLE, C.POINTER(Memory), W.DWORD)(handle, C.byref(memory), memory.cb))
            value['private_commit'] = int(memory.PrivateUsage)
        return value
    finally:
        close(handle)


def pids(job):
    class List(C.Structure):
        _fields_ = [('NumberOfAssignedProcesses', W.DWORD), ('NumberOfProcessIdsInList', W.DWORD), ('ProcessIdList', SIZE * 4096)]
    value, length = List(), W.DWORD()
    ok(query_job(job, 3, C.byref(value), C.sizeof(value), C.byref(length)))
    return sorted(set(int(x) for x in value.ProcessIdList[:value.NumberOfProcessIdsInList]))


def observe_job(name, measure=False):
    job = open_job(4, False, name)
    if not job:
        if C.get_last_error() == 2:
            return {'exists': False, 'members': [], 'idle_proven': False}
        raise C.WinError(C.get_last_error())
    try:
        members = []
        for pid in pids(job):
            try:
                members.append(pid_identity(pid, job, measure=measure))
            except OSError:
                return {'exists': True, 'members': members, 'idle_proven': False, 'race': 'member changed while observing'}
        return {'exists': True, 'members': members, 'idle_proven': not members}
    finally:
        close(job)


def reclaim_idle_job(name, expected):
    job = ok(open_job(4 | 8, False, name))
    try:
        members = pids(job)
        if not members:
            raise ValueError('Job already empty; use its actual exit event rather than another stop')
        if expected['pid'] not in members:
            raise ValueError('Root is not in the registered Job; no termination')
        actual = pid_identity(expected['pid'], job)
        if actual['created'] != expected['created'] or not actual['running']:
            raise ValueError('PID identity/usage changed; no termination')
        ok(terminate_job(job, 0))
        return {'termination_requested': True, 'owned_job_pids': members,
                'completion': 'not yet; original launcher must verify whole-tree exit'}
    finally:
        close(job)


class Job:
    def __init__(self, name):
        self.name, self.process, self.threads, self.members = name, None, [], {}
        self.observed_handles = {}
        self.job = ok(create_job(None, name))
        if C.get_last_error() == 183:
            close(self.job)
            raise ValueError('Job identity already exists; never attach an unrelated process')
        self.port = ok(create_port(W.HANDLE(-1), None, 0, 1))
        value = Port(1, self.port)
        ok(set_job(self.job, 7, C.byref(value), C.sizeof(value)))

    def launch(self, argv, cwd, logs):
        import msvcrt
        files = [open(os.devnull, 'rb'), open(logs / 'stdout.txt', 'wb'), open(logs / 'stderr.txt', 'wb')]
        try:
            for f in files:
                os.set_handle_inheritable(msvcrt.get_osfhandle(f.fileno()), True)
            start = Startup(cb=C.sizeof(Startup), dwFlags=0x100,
                hStdInput=msvcrt.get_osfhandle(files[0].fileno()), hStdOutput=msvcrt.get_osfhandle(files[1].fileno()),
                hStdError=msvcrt.get_osfhandle(files[2].fileno()))
            proc = Process()
            command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            ok(create_process(argv[0], command, None, None, True, 0x4 | 0x08000000, None, str(cwd), C.byref(start), C.byref(proc)))
            self.process = proc
            try:
                ok(assign_job(self.job, proc.hProcess))
            except OSError:
                api(k32, 'TerminateProcess', W.BOOL, W.HANDLE, W.UINT)(proc.hProcess, 1)
                raise
            # Caller persists this identity before resume; crash never implies release.
            return identity(proc.hProcess, proc.dwProcessId)
        finally:
            for f in files:
                f.close()

    def start(self):
        require = resume(self.process.hThread)
        if require == 0xFFFFFFFF:
            raise C.WinError(C.get_last_error())
        close(self.process.hThread)
        self.process.hThread = None

    def sample(self):
        current = pids(self.job)
        private = []
        for pid in current:
            handle = open_process(0x0400 | 0x0010 | 0x100000, False, pid)
            if not handle:
                continue  # An exit race is not evidence of freed permits.
            try:
                member = W.BOOL()
                ok(is_in_job(handle, self.job, C.byref(member)))
                if not member.value:
                    continue
                value = identity(handle, pid)
                memory = Memory(cb=C.sizeof(Memory))
                ok(api(psapi, 'GetProcessMemoryInfo', W.BOOL, W.HANDLE, C.POINTER(Memory), W.DWORD)(handle, C.byref(memory), memory.cb))
                value['private_commit'] = int(memory.PrivateUsage)
                key = (pid, value['created'])
                self.members[key] = value
                if key not in self.observed_handles:
                    self.observed_handles[key] = handle
                    handle = None
                private.append(value['private_commit'])
            except OSError:
                continue  # Attribution unavailable is not a reason to release a live tree.
            finally:
                if handle:
                    close(handle)
        return {'unique_pids': current, 'private_commit_total': sum(private) if len(private) == len(current) else None,
                'measurement_complete': len(private) == len(current), 'shared_working_sets_summed': False,
                'private_resident': 'not measured; physical admission uses system available memory'}

    def wait(self, on_event=None):
        while True:
            message, key, pointer = W.DWORD(), SIZE(), C.c_void_p()
            ok(get_packet(self.port, C.byref(message), C.byref(key), C.byref(pointer), 0xFFFFFFFF))
            measured = self.sample()
            if on_event:
                on_event(int(message.value), measured)
            if message.value == 4 and not pids(self.job):
                if identity(self.process.hProcess, self.process.dwProcessId)['running'] or any(
                        identity(handle, key[0])['running'] for key, handle in self.observed_handles.items()):
                    raise ValueError('Job empty notification does not prove observed process handles exited')
                code = identity(self.process.hProcess, self.process.dwProcessId)['exit_code']
                return {'root_exit_code': code, 'tree_empty': True, 'members': list(self.members.values()),
                        'completion_event': 'JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO'}

    def close(self):
        for handle in self.observed_handles.values():
            close(handle)
        self.observed_handles.clear()
        if self.process:
            if self.process.hThread:
                close(self.process.hThread)
            close(self.process.hProcess)
        close(self.port)
        close(self.job)  # KILL_ON_JOB_CLOSE is deliberately not set.
