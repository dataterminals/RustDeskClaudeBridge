"""Running the RustDesk executable, elevated and not.

Two facts about RustDesk on Windows shape this module.

**It answers on stdout.** ``RustDesk.exe`` is a GUI-subsystem binary, so silence
would be the reasonable expectation -- but ``--version`` and ``--get-id`` really
do print and exit 0. Everything in the read-only layer rests on that.

**Session verbs do not start a session in this process.** ``--connect``,
``--terminal``, ``--file-transfer`` and friends are translated into a
``rustdesk://`` URI and posted to the *already running* instance via a window
message (see ``core_main_invoke_new_connection`` in core_main.rs). The process
you launch exits immediately, and a tab opens in the app that was already there.
So a zero exit code means "the request was handed over", not "the session
connected" -- read the log for that, via ``state.py``.
"""

import os
import re
import subprocess
import tempfile

from . import policy
from .errors import RustDeskError, RustDeskTimeout

_CREATE_NO_WINDOW = 0x08000000

# Option values are pasted into a temporary batch file when elevating, where a
# double quote would end the argument and a caret or ampersand would be parsed
# by cmd. Percent signs are escapable; the rest are simply refused.
_UNSAFE_VALUE = re.compile(r'["\r\n\x00]')


class RustDesk:
    """A thin, policed wrapper around the RustDesk executable."""

    def __init__(self, exe, timeout=60):
        self.exe = exe
        self.timeout = timeout

    # -- plain invocation -------------------------------------------------
    def run(self, args, timeout=None, check=True):
        """Run RustDesk with ``args`` and return its stdout, stripped.

        Refuses any argument vector containing a never-allowed verb, before the
        process is created.
        """
        args = policy.assert_args_allowed(list(args))
        command = [self.exe] + [str(a) for a in args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise RustDeskTimeout(
                "RustDesk did not exit within %ss: %s"
                % (timeout or self.timeout, " ".join(args)),
                args=args,
            ) from exc
        except OSError as exc:
            raise RustDeskError(
                "Could not run %s: %s" % (self.exe, exc), args=args
            ) from exc

        output = (completed.stdout or "").strip()
        if check and completed.returncode != 0:
            raise RustDeskError(
                "RustDesk exited %d for: %s\n%s"
                % (completed.returncode, " ".join(args),
                   output or (completed.stderr or "").strip()),
                exit_code=completed.returncode,
                output=output,
                args=args,
            )
        return output

    # -- elevated invocation ----------------------------------------------
    def run_elevated(self, args, timeout=None):
        """Run RustDesk elevated and return its stdout.

        ``--option`` writes are gated in RustDesk itself on
        ``is_installed() && is_root()``, so a settings write has no unelevated
        path -- it prompts for UAC every time, by design.

        An elevated child runs in a different security context, so its stdout
        cannot simply be piped back. Instead a temporary batch file performs the
        redirection on the elevated side and this process reads the file.
        """
        args = policy.assert_args_allowed(list(args))
        for arg in args:
            if _UNSAFE_VALUE.search(str(arg)):
                raise RustDeskError(
                    "Refusing to elevate with an argument containing a quote or "
                    "newline: %r" % (arg,),
                    args=args,
                )

        workdir = tempfile.mkdtemp(prefix="rdbridge-")
        out_path = os.path.join(workdir, "out.txt")
        rc_path = os.path.join(workdir, "rc.txt")
        bat_path = os.path.join(workdir, "run.cmd")

        quoted = " ".join('"%s"' % str(a).replace("%", "%%") for a in args)
        script = (
            "@echo off\r\n"
            '"%s" %s 1>"%s" 2>&1\r\n'
            'echo %%ERRORLEVEL%%>"%s"\r\n'
        ) % (self.exe, quoted, out_path, rc_path)
        with open(bat_path, "w", encoding="utf-8") as handle:
            handle.write(script)

        exit_code = _shell_execute_wait(
            "cmd.exe", '/c ""%s""' % bat_path, timeout or self.timeout
        )

        output = ""
        if os.path.isfile(out_path):
            with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
                output = handle.read().strip()
        inner_rc = None
        if os.path.isfile(rc_path):
            with open(rc_path, "r", encoding="utf-8", errors="replace") as handle:
                try:
                    inner_rc = int(handle.read().strip())
                except ValueError:
                    inner_rc = None

        for path in (out_path, rc_path, bat_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass

        if exit_code != 0 and inner_rc is None:
            raise RustDeskError(
                "Elevated RustDesk call failed (the UAC prompt was most likely "
                "declined): %s" % " ".join(args),
                exit_code=exit_code,
                args=args,
            )
        if inner_rc not in (None, 0):
            raise RustDeskError(
                "RustDesk exited %d for: %s\n%s" % (inner_rc, " ".join(args), output),
                exit_code=inner_rc,
                output=output,
                args=args,
            )

        # RustDesk reports these as normal output, not as a non-zero exit code.
        for refusal in ("Settings are disabled!",
                        "Installation and administrative privileges required!"):
            if refusal in output:
                raise RustDeskError(
                    "RustDesk refused the call: %s" % refusal,
                    output=output,
                    args=args,
                )
        return output

    # -- convenience ------------------------------------------------------
    def version(self):
        return self.run(["--version"])

    def my_id(self):
        """This machine's own RustDesk ID."""
        return self.run(["--get-id"])


def _shell_execute_wait(file, parameters, timeout):
    """ShellExecuteEx with the 'runas' verb, waiting for the child to exit."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NO_CONSOLE = 0x00008000
    SW_HIDE = 0
    WAIT_TIMEOUT = 0x00000102

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = parameters
    info.nShow = SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        last_error = ctypes.windll.kernel32.GetLastError()
        detail = (" The UAC prompt was declined."
                  if last_error == 1223 else "")
        raise RustDeskError(
            "Could not start an elevated process (Windows error %d).%s"
            % (last_error, detail)
        )

    handle = info.hProcess
    try:
        waited = ctypes.windll.kernel32.WaitForSingleObject(
            handle, int(timeout * 1000)
        )
        if waited == WAIT_TIMEOUT:
            raise RustDeskTimeout(
                "The elevated RustDesk call did not finish within %ss." % timeout
            )
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return int(code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
