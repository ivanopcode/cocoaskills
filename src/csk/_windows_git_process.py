"""Windows bounded child execution; attach before any child code can run."""
from __future__ import annotations

import ctypes
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_TH32CS_SNAPTHREAD = 4
_THREAD_SUSPEND_RESUME = 2
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]

class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]

class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]

class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("owner_process_id", ctypes.c_uint32),
        ("base_priority", ctypes.c_long),
        ("delta_priority", ctypes.c_long),
        ("flags", ctypes.c_uint32),
    ]

def _windows_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

def _close_windows_handle(handle: int) -> None:
    if not handle:
        return
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    if not close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

def _resume_windows_process(pid: int) -> None:
    kernel32 = _windows_kernel32()
    snapshot_fn = kernel32.CreateToolhelp32Snapshot
    snapshot_fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    snapshot_fn.restype = ctypes.c_void_p
    first = kernel32.Thread32First
    first.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    first.restype = ctypes.c_int
    following = kernel32.Thread32Next
    following.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    following.restype = ctypes.c_int
    open_thread = kernel32.OpenThread
    open_thread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_thread.restype = ctypes.c_void_p
    resume = kernel32.ResumeThread
    resume.argtypes = [ctypes.c_void_p]
    resume.restype = ctypes.c_uint32
    snapshot_value = snapshot_fn(_TH32CS_SNAPTHREAD, 0)
    if snapshot_value == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    snapshot = int(snapshot_value)
    resumed = 0
    try:
        entry = _ThreadEntry32()
        entry.size = ctypes.sizeof(entry)
        more = bool(first(snapshot, ctypes.byref(entry)))
        while more:
            if entry.owner_process_id == pid:
                thread_value = open_thread(
                    _THREAD_SUSPEND_RESUME,
                    False,
                    entry.thread_id,
                )
                if not thread_value:
                    raise ctypes.WinError(  # type: ignore[attr-defined]
                        ctypes.get_last_error()  # type: ignore[attr-defined]
                    )
                thread = int(thread_value)
                try:
                    previous = resume(thread)
                    if previous == 0xFFFFFFFF:
                        raise ctypes.WinError(  # type: ignore[attr-defined]
                            ctypes.get_last_error()  # type: ignore[attr-defined]
                        )
                    resumed += 1
                finally:
                    _close_windows_handle(thread)
            more = bool(following(snapshot, ctypes.byref(entry)))
    finally:
        _close_windows_handle(snapshot)
    if resumed == 0:
        raise OSError("suspended worker has no resumable thread")


def _end_job(kernel: Any, job: int) -> None:
    # Retain process handles before termination: active-process accounting can
    # reach zero before process teardown releases inherited pipes/directories.
    class ProcessIds(ctypes.Structure):
        _fields_ = [("assigned", ctypes.c_uint32), ("count", ctypes.c_uint32),
                    ("ids", ctypes.c_size_t * 4096)]
    query = kernel.QueryInformationJobObject
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    query.restype = ctypes.c_int
    terminate = kernel.TerminateJobObject
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int
    open_process = kernel.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    wait = kernel.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    handles = []
    try:
        ids = ProcessIds()
        if not query(job, 3, ctypes.byref(ids), ctypes.sizeof(ids), None):
            raise ctypes.WinError()  # type: ignore[attr-defined]
        for pid in ids.ids[:ids.count]:
            handle = open_process(0x100000, False, pid)
            if handle:
                handles.append(int(handle))
        if not terminate(job, 1):
            raise ctypes.WinError()  # type: ignore[attr-defined]
        deadline = time.monotonic() + 2
        for handle in handles:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if wait(handle, remaining) != 0:
                raise OSError("Windows Git tree did not terminate within cleanup budget")
    finally:
        for handle in handles:
            _close_windows_handle(handle)


def run(command: Sequence[str], *, cwd: Path | None = None,
        env: Mapping[str, str] | None = None, stdin: int,
        stdout: int, stderr: int, timeout: float, check: bool) -> subprocess.CompletedProcess[bytes]:
    kernel = _windows_kernel32()
    create = kernel.CreateJobObjectW
    create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    job = create(None, None)
    if not job:
        raise ctypes.WinError()  # type: ignore[attr-defined]
    process = None
    try:
        info = _JobExtendedLimitInformation()
        info.basic_limit_information.limit_flags = 0x2000  # KILL_ON_JOB_CLOSE
        configure = kernel.SetInformationJobObject
        configure.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        configure.restype = ctypes.c_int
        if not configure(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError()  # type: ignore[attr-defined]
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin,
                                   stdout=stdout, stderr=stderr, creationflags=4)
        assign = kernel.AssignProcessToJobObject
        assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        assign.restype = ctypes.c_int
        if not assign(job, int(process._handle)):  # type: ignore[attr-defined]
            raise ctypes.WinError()  # type: ignore[attr-defined]
        _resume_windows_process(process.pid)
        output, errors = process.communicate(timeout=timeout)
        status = process.returncode
        if check and status:
            raise subprocess.CalledProcessError(status, command, output, errors)
        return subprocess.CompletedProcess(command, status, output, errors)
    finally:
        # This handle is non-inheritable. Closing it ends every descendant,
        # including remote helpers retaining the captured stderr pipe.
        try:
            _end_job(kernel, int(job))
        finally:
            _close_windows_handle(int(job))
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
