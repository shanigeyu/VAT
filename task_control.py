"""Cooperative cancellation for a desktop job and the processes it starts."""

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar


class JobCancelled(BaseException):
    """A user stop must bypass handlers that retry ordinary provider errors."""


class JobControl:
    def __init__(self):
        self.event = threading.Event()

    @property
    def cancelled(self):
        return self.event.is_set()

    def cancel(self):
        self.event.set()

    def check(self):
        if self.cancelled:
            raise JobCancelled()


_current = ContextVar("vat_job", default=None)


@contextmanager
def running(control):
    token = _current.set(control)
    try:
        yield
    finally:
        _current.reset(token)


def checkpoint():
    control = _current.get()
    if control is not None:
        control.check()


def sleep(seconds):
    control = _current.get()
    if control is None:
        time.sleep(seconds)
    else:
        control.event.wait(seconds)
        control.check()


def network_call(request, *, close=None):
    """Wait interruptibly for network I/O. The callback must not write files.

    A request already accepted by a remote service may still finish there. Its
    late response cannot advance the cancelled job or overwrite a saved result.
    """
    control = _current.get()
    if control is None:
        return request()
    control.check()
    finished = threading.Event()
    result = []

    def fetch():
        try:
            with running(control):
                control.check()
                result.append((True, request()))
        except BaseException as error:
            result.append((False, error))
        finally:
            finished.set()

    threading.Thread(target=fetch, daemon=True, name="VAT-network").start()
    try:
        while not finished.wait(.05):
            control.check()
        control.check()
    except JobCancelled:
        if close is not None:
            # Closing a socket must never block Tk's event loop or saving.
            def disconnect():
                try:
                    close()
                except OSError:
                    pass
            threading.Thread(target=disconnect, daemon=True, name="VAT-disconnect").start()
        raise
    ok, value = result[0]
    if not ok:
        raise value
    return value


def stop_process(process):
    """Stop this job's process tree; never target a shared model service."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


@contextmanager
def process(args, **kwargs):
    checkpoint()
    if os.name != "nt":
        kwargs["start_new_session"] = True
    child = subprocess.Popen(args, **kwargs)
    try:
        yield child
    finally:
        if child.poll() is None:
            stop_process(child)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def run_process(args, *, input=None, timeout=None, capture_output=False, check=False, **kwargs):
    control = _current.get()
    if control is None:
        return subprocess.run(args, input=input, timeout=timeout,
                              capture_output=capture_output, check=check, **kwargs)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    deadline = time.monotonic() + timeout if timeout is not None else None
    with process(args, **kwargs) as child:
        payload = input
        while True:
            control.check()
            remaining = deadline - time.monotonic() if deadline is not None else .1
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                out, err = child.communicate(payload, timeout=min(.1, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        result = subprocess.CompletedProcess(args, child.returncode, out, err)
        if check:
            result.check_returncode()
        return result
