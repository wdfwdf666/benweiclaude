"""Bounded, shell-free child process execution."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from typing import Any, BinaryIO

from .backends import BubblewrapBackend, SandboxCapabilities
from .policy import SandboxPolicy
from .result import SandboxResult


def parse_command(command: str) -> list[str]:
    """Parse a legacy command string without invoking a command shell."""
    if not command.strip():
        raise ValueError("Command is empty")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        count = ctypes.c_int()
        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
        pointer = command_line_to_argv(command, ctypes.byref(count))
        if not pointer:
            raise ValueError("Command line parsing failed")
        try:
            argv = [pointer[index] for index in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(pointer)
    else:
        argv = shlex.split(command, posix=True)
    if not argv:
        raise ValueError("Command is empty")
    return argv


class SandboxProcess:
    """Popen wrapper that owns the operating-system process group."""

    def __init__(self, process: subprocess.Popen[bytes], job_handle=None) -> None:
        self._process = process
        self._job_handle = job_handle

    def __getattr__(self, name):
        return getattr(self._process, name)

    @property
    def stdin(self):
        return self._process.stdin

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def stderr(self):
        return self._process.stderr

    def kill_tree(self) -> None:
        if self._process.poll() is not None:
            return
        if os.name == "nt":
            if self._job_handle is not None:
                import ctypes

                ctypes.windll.kernel32.TerminateJobObject(self._job_handle, 1)
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False,
                )
        else:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
            except ProcessLookupError:
                pass

    def close(self) -> None:
        if self._process.poll() is None:
            self.kill_tree()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2)
        if self._job_handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._job_handle)
            self._job_handle = None


class SandboxRunner:
    """Start processes with minimal environment and bounded lifetime/output."""

    def capabilities(self) -> SandboxCapabilities:
        if os.name == "nt":
            return SandboxCapabilities(
                backend="windows-job-object", process_tree=True, memory_limit=True,
                process_limit=True, filesystem_isolation=False, network_isolation=False,
            )
        return BubblewrapBackend().capabilities()

    def environment(self, policy: SandboxPolicy, additions: dict[str, str] | None = None) -> dict[str, str]:
        allowed = {name.upper() for name in policy.allowed_environment}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update({
            "CODEPILOT_PROJECT_DIR": str(policy.workspace),
            "CLAUDE_PROJECT_DIR": str(policy.workspace),
            "CODEPILOT_SANDBOX_PROFILE": policy.profile,
            "CODEPILOT_NETWORK_MODE": policy.network_mode,
        })
        if policy.network_mode == "none":
            blocked_proxy = "http://127.0.0.1:9"
            env.update({
                "HTTP_PROXY": blocked_proxy, "HTTPS_PROXY": blocked_proxy,
                "ALL_PROXY": blocked_proxy, "NO_PROXY": "",
            })
        if additions:
            env.update({str(key): str(value) for key, value in additions.items()})
        return env

    def spawn(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        env_additions: dict[str, str] | None = None,
    ) -> SandboxProcess:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("Sandbox argv must contain non-empty strings")
        if os.name == "nt" and policy.strict_isolation and (
            policy.filesystem_mode != "unrestricted" or policy.network_mode != "unrestricted"
        ):
            raise RuntimeError("Strict filesystem and network isolation requires the Windows AppContainer backend")
        if os.name != "nt":
            argv = BubblewrapBackend().wrap(argv, policy)
        kwargs: dict[str, Any] = {
            "cwd": policy.workspace,
            "env": self.environment(policy, env_additions),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = self._unix_limits(policy)
        process: subprocess.Popen[bytes] = subprocess.Popen(argv, **kwargs)
        return SandboxProcess(process, self._windows_job(process, policy) if os.name == "nt" else None)

    def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: str | None = None,
        env_additions: dict[str, str] | None = None,
    ) -> SandboxResult:
        started = time.perf_counter()
        process = self.spawn(argv, policy, env_additions=env_additions)
        stdout = bytearray()
        stderr = bytearray()
        truncated = threading.Event()
        threads = [
            threading.Thread(
                target=self._drain,
                args=(process.stdout, stdout, policy.max_output_bytes, truncated),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(process.stderr, stderr, policy.max_output_bytes, truncated),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        if process.stdin is not None:
            try:
                if stdin is not None:
                    process.stdin.write(stdin.encode("utf-8"))
                process.stdin.close()
            except BrokenPipeError:
                pass
        timed_out = False
        try:
            process.wait(timeout=policy.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill_tree()
            process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=2)
        result = SandboxResult(
            argv=tuple(argv),
            returncode=None if timed_out else process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_ms=(time.perf_counter() - started) * 1000,
            timed_out=timed_out,
            output_truncated=truncated.is_set(),
        )
        process.close()
        return result

    @staticmethod
    def _drain(stream: BinaryIO | None, destination: bytearray, limit: int,
               truncated: threading.Event) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            remaining = limit - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated.set()

    @staticmethod
    def _unix_limits(policy: SandboxPolicy):
        def apply() -> None:
            import importlib

            resource: Any = importlib.import_module("resource")
            memory = policy.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_NPROC, (policy.max_processes, policy.max_processes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return apply

    @staticmethod
    def _windows_job(process: subprocess.Popen[bytes], policy: SandboxPolicy):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            0x00002000  # KILL_ON_JOB_CLOSE
            | 0x00000008  # ACTIVE_PROCESS
            | 0x00000200  # JOB_MEMORY
        )
        limits.BasicLimitInformation.ActiveProcessLimit = policy.max_processes
        limits.JobMemoryLimit = policy.max_memory_mb * 1024 * 1024
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ) or not kernel32.AssignProcessToJobObject(
            handle, wintypes.HANDLE(process._handle),  # type: ignore[attr-defined]
        ):
            kernel32.CloseHandle(handle)
            return None
        return handle
