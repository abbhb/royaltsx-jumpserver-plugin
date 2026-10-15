#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drives src/jms_terminal.sh through a real pty.

The wrapper's whole job is to survive ssh exiting, so the only way to test it
honestly is to give it a terminal and a stand-in ssh that exits on demand.

Run with:  python3 tests/test_terminal_wrapper.py
"""

import os
import pathlib
import pty
import shutil
import sys
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "src" / "jms_terminal.sh"

LOGIN = "alice@root@10.0.0.10"
HOST = "jumpserver.example.com"
PORT = "2222"


class FakeSsh:
    """Stands in for /usr/bin/ssh and records how often it was invoked."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="jms-fake-ssh-")
        self.counter = os.path.join(self.dir, "calls")
        path = os.path.join(self.dir, "ssh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/bash\n"
                'echo "$@" >> ' + self.counter + "\n"
                'echo "FAKE-SSH-RAN"\n'
                "exit 0\n"
            )
        os.chmod(path, 0o755)

    def calls(self):
        if not os.path.exists(self.counter):
            return []
        with open(self.counter, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def drive(keys, fake, timeout=10.0):
    """Run the wrapper under a pty, send `keys`, return everything it printed."""
    pid, fd = pty.fork()
    if pid == 0:                                  # child
        os.environ["PATH"] = fake.dir + os.pathsep + os.environ["PATH"]
        os.execv("/bin/bash", ["/bin/bash", str(SCRIPT), LOGIN, HOST, PORT])
        os._exit(127)

    output = bytearray()
    deadline = time.time() + timeout
    sent = 0

    while time.time() < deadline:
        # Feed the next key once the wrapper looks like it is waiting.
        if sent < len(keys) and b"\xe2\x94\x81" in bytes(output):
            os.write(fd, keys[sent].encode())
            sent += 1
            time.sleep(0.3)
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if b"FAKE-SSH-RAN" in chunk and sent == 0 and len(keys) == 0:
            break

    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return output.decode("utf-8", "replace")


class TestTerminalWrapper(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "wrapper script is missing")
        self.fake = FakeSsh()

    def tearDown(self):
        self.fake.cleanup()

    def test_quitting_runs_ssh_once(self):
        output = drive("q", self.fake)
        self.assertIn("FAKE-SSH-RAN", output)
        self.assertEqual(len(self.fake.calls()), 1)

    def test_r_reconnects(self):
        output = drive("rq", self.fake)
        self.assertEqual(len(self.fake.calls()), 2,
                         "R should have dialled in a second time")
        self.assertIn("重连", output)

    def test_two_reconnects(self):
        output = drive("rrq", self.fake)
        self.assertEqual(len(self.fake.calls()), 3)

    def test_ssh_receives_the_direct_login_username(self):
        drive("q", self.fake)
        call = self.fake.calls()[0]
        # koko needs the whole "user@account@asset" string as the login name.
        self.assertIn("-l " + LOGIN, call)
        self.assertIn("-p " + PORT, call)
        self.assertIn(HOST, call)
        self.assertIn("-tt", call, "a pty must be forced for an interactive shell")

    def test_disconnect_banner_is_shown(self):
        output = drive("q", self.fake)
        self.assertIn("连接已断开", output)
        self.assertIn("R", output)
        self.assertIn("Q", output)


if __name__ == "__main__":
    if not SCRIPT.exists():
        sys.exit("cannot find " + str(SCRIPT))
    unittest.main(verbosity=2)
