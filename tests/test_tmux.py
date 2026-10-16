"""The tmux wrapper: argv construction, and one real session.

The session moving into tmux is what makes it outlive the server process. The
argv is checked as a pure value because getting it wrong fails at runtime in a
way that looks like the child crashing.
"""

import shutil
import subprocess

import pytest

from dsterm.grid import COLS, ROWS
from dsterm.session import _parse_tmux_target
from dsterm.serve import TMUX_CONF, TMUX_SOCKET, tmux_command


class TestTheParserCanReadWhatTheBuilderWrites:
    """`Session` reads the socket and session name back out of the argv.

    That is a deliberate choice -- the argv is the thing that is actually true,
    and it means a caller who hand-builds a tmux command line gets the right
    pane without knowing there is a second parameter to keep in step. But it
    couples two functions through a list of strings with nothing checking the
    coupling, and the failure is silent: a reordered or renamed flag makes the
    parser return None, `_pane_alt_screen` then declines to query anything, the
    cached opening guess of True stands forever, and the handheld sends PageUp
    at a shell prompt for the rest of the session. No exception, no log line.
    """

    def test_the_shipped_argv_parses_back_to_its_own_socket_and_session(self):
        argv = tmux_command("dsterm", ["claude"], COLS, ROWS)
        assert _parse_tmux_target(argv) == (TMUX_SOCKET, "dsterm")

    def test_a_test_socket_argv_parses_back_too(self):
        argv = tmux_command("handheld", ["bash"], COLS, ROWS, socket="dstermtest")
        assert _parse_tmux_target(argv) == ("dstermtest", "handheld")

    def test_a_child_argument_is_never_mistaken_for_the_session_name(self):
        # The child's own flags sit in the same list. A child invoked with -s
        # must not capture the session name, or we query a pane that does not
        # exist and silently keep the opening guess.
        argv = tmux_command("dsterm", ["some-tool", "-s", "not-the-session"],
                            COLS, ROWS)
        assert _parse_tmux_target(argv) == (TMUX_SOCKET, "dsterm")


class TestArgv:
    def test_runs_on_a_dedicated_socket_with_the_repo_config(self):
        # -L keeps this away from any personal or agent tmux server, so nothing
        # here can disturb a session someone else is using.
        argv = tmux_command("dsterm", ["claude"], COLS, ROWS)
        assert argv[:5] == ["tmux", "-L", TMUX_SOCKET, "-f", str(TMUX_CONF)]

    def test_attaches_if_the_session_exists(self):
        # -A is the whole "reconnect automatically" behaviour: the session lives
        # in the tmux server, so the DS rejoins rather than starting over.
        argv = tmux_command("dsterm", ["claude"], COLS, ROWS)
        assert "new-session" in argv
        assert "-A" in argv

    def test_size_comes_from_the_grid_not_a_literal(self):
        # If these ever drift from the protocol's geometry the emulator and the
        # handheld disagree about where every cell is.
        argv = tmux_command("dsterm", ["claude"], COLS, ROWS)
        assert argv[argv.index("-x") + 1] == str(COLS)
        assert argv[argv.index("-y") + 1] == str(ROWS)

    def test_the_command_is_last_and_intact(self):
        # tmux treats everything after its own flags as the command; a flag
        # appended after it would be swallowed as an argument to the child.
        argv = tmux_command("dsterm", ["claude", "--resume", "abc"], COLS, ROWS)
        assert argv[-3:] == ["claude", "--resume", "abc"]

    def test_the_session_name_is_used(self):
        argv = tmux_command("handheld", ["bash"], COLS, ROWS)
        assert argv[argv.index("-s") + 1] == "handheld"


class TestConfig:
    def test_window_size_is_manual(self):
        # The one line that makes tmux usable here. Without it a PC attaching
        # renegotiates the size and collapses the handheld's window -- which is
        # exactly why tmux was rejected as a multiplexer before.
        #
        # Applied from a hook: setting it at config-parse time, before a session
        # exists, kills the tmux server outright.
        assert "window-size manual" in TMUX_CONF.read_text()
        assert "set-hook" in TMUX_CONF.read_text()

    def test_the_config_does_not_kill_the_server(self):
        # The failure mode this guards is brutal to diagnose: one bad option
        # makes tmux exit with "server exited unexpectedly" and no mention of
        # which line, so the child simply never starts.
        import shutil
        if shutil.which("tmux") is None:
            pytest.skip("tmux not installed")

        sock = "dsterm-conf-check"
        subprocess.run(["tmux", "-L", sock, "-f", str(TMUX_CONF),
                        "new-session", "-d", "-s", "probe", "sleep", "5"],
                       capture_output=True, timeout=10)
        alive = subprocess.run(["tmux", "-L", sock, "ls"],
                               capture_output=True, timeout=10)
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
        assert alive.returncode == 0, "the config prevented the server starting"

    def test_the_status_line_is_off(self):
        # 24 rows total; a status line spends one of them on tmux's chrome.
        assert "set -g status off" in TMUX_CONF.read_text()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestAgainstRealTmux:
    """Not a mock. tmux is the thing being relied on, so run it."""

    SOCKET = "dsterm-test"

    def teardown_method(self):
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-server"],
                       capture_output=True)

    def test_a_session_is_created_at_the_handheld_geometry(self):
        argv = tmux_command("t", ["sleep", "30"], COLS, ROWS, socket=self.SOCKET)
        # -d so it does not try to attach to this test's absent terminal.
        subprocess.run(argv[:argv.index("new-session") + 1] + ["-d"]
                       + argv[argv.index("new-session") + 1:],
                       capture_output=True, timeout=10)

        out = subprocess.run(
            ["tmux", "-L", self.SOCKET, "display-message", "-p", "-t", "t",
             "#{window_width}x#{window_height}"],
            capture_output=True, text=True, timeout=10)
        assert out.stdout.strip() == f"{COLS}x{ROWS}"

    def test_the_size_is_pinned_manual_once_the_session_exists(self):
        # The hook has to have actually fired. If it did not, an attaching PC
        # renegotiates the window and the handheld's screen collapses.
        argv = tmux_command("t", ["sleep", "30"], COLS, ROWS, socket=self.SOCKET)
        subprocess.run(argv[:argv.index("new-session") + 1] + ["-d"]
                       + argv[argv.index("new-session") + 1:],
                       capture_output=True, timeout=10)

        # Queried on the WINDOW, not globally (-w -t rather than -wg). The hook
        # sets it per window now, because the global form kills tmux 3.4 the
        # moment a second window is created. The property this test protects is
        # unchanged -- the window must be pinned -- only the scope it lives in
        # moved, and the global value is now tmux's default.
        out = subprocess.run(
            ["tmux", "-L", self.SOCKET, "show-options", "-w", "-t", "t:0",
             "window-size"],
            capture_output=True, text=True, timeout=10)
        assert out.stdout.split()[-1] == "manual"

    def test_the_session_outlives_the_process_that_made_it(self):
        # The reason for all of this: restarting the server must not destroy
        # whatever Claude Code was in the middle of.
        argv = tmux_command("t", ["sleep", "30"], COLS, ROWS, socket=self.SOCKET)
        subprocess.run(argv[:argv.index("new-session") + 1] + ["-d"]
                       + argv[argv.index("new-session") + 1:],
                       capture_output=True, timeout=10)

        out = subprocess.run(["tmux", "-L", self.SOCKET, "ls"],
                             capture_output=True, text=True, timeout=10)
        assert "t:" in out.stdout


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestASecondWindowDoesNotKillTheServer:
    """The crash that cost three real sessions.

    `window-size manual` set globally makes tmux 3.4 exit with "server exited
    unexpectedly" the instant a second window is created. From the handheld that
    looked like Ctrl-B c opening a window and bouncing straight back to Claude
    Code -- because the tmux server had died and the service had started a fresh
    session, which is exactly what the service is supposed to do.

    The config now sets it per window. This runs the real shipped config on a
    throwaway socket and requires two windows to coexist, because the failure is
    invisible to any test that only reads the file: the option is spelled almost
    identically either way, and only tmux can tell you which one is fatal.
    """

    SOCKET = "dstermtest-windows"

    def _tmux(self, *args, conf=False):
        base = ["tmux", "-L", self.SOCKET]
        if conf:
            base += ["-f", str(TMUX_CONF)]
        return subprocess.run(base + list(args), capture_output=True, text=True)

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._tmux("kill-server")
        yield
        self._tmux("kill-server")

    def test_two_windows_coexist_under_the_shipped_config(self):
        self._tmux("new-session", "-d", "-s", "t",
                   "-x", str(COLS), "-y", str(ROWS), "sleep", "30", conf=True)
        made = self._tmux("new-window", "-d", "-t", "t:", "-n", "second")
        assert "server exited unexpectedly" not in (made.stderr + made.stdout), (
            "a second window killed the tmux server -- window-size is global again"
        )
        listed = self._tmux("list-windows", "-t", "t")
        assert listed.returncode == 0, listed.stderr
        assert len(listed.stdout.strip().splitlines()) == 2

    def test_both_windows_keep_the_handheld_geometry(self):
        # The reason the option exists at all. If the fix had been to simply
        # drop it, this is what would have been lost, and the loss would only
        # show up when someone attached from a PC.
        self._tmux("new-session", "-d", "-s", "t",
                   "-x", str(COLS), "-y", str(ROWS), "sleep", "30", conf=True)
        self._tmux("new-window", "-d", "-t", "t:", "-n", "second")
        sizes = self._tmux("list-windows", "-t", "t",
                           "-F", "#{window_width}x#{window_height}")
        assert sizes.stdout.split() == [f"{COLS}x{ROWS}"] * 2, sizes.stdout

    def test_the_config_never_sets_window_size_globally(self):
        # Belt and braces next to the behavioural test: -g is the exact spelling
        # that is fatal, and a future edit is likelier to reintroduce it by
        # copying a line than by redesigning the hook.
        text = TMUX_CONF.read_text()
        for line in text.splitlines():
            bare = line.strip()
            if bare.startswith("#"):
                continue
            assert "-g window-size" not in bare, (
                f"global window-size is fatal on a second window: {bare!r}"
            )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestAGridChangeResizesTheSessionItAttachesTo:
    """`new-session -A` ignores -x/-y when the session is already there, and
    `window-size manual` then holds every window at its old size: after
    `--grid 64x32` and a restart, the 64x32 card was drawn a 42x24 island.
    serve.main fits the existing session before the child attaches."""

    SOCKET = "dstermtest-fit"

    def _tmux(self, *args, conf=False):
        base = ["tmux", "-L", self.SOCKET]
        if conf:
            base += ["-f", str(TMUX_CONF)]
        return subprocess.run(base + list(args), capture_output=True, text=True,
                              timeout=10)

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._tmux("kill-server")
        yield
        self._tmux("kill-server")

    def _sizes(self):
        out = self._tmux("list-windows", "-t", "t",
                         "-F", "#{window_width}x#{window_height}")
        return out.stdout.split()

    def test_every_window_and_every_later_one_takes_the_new_size(self):
        from dsterm.serve import fit_session

        self._tmux("new-session", "-d", "-s", "t", "-x", "42", "-y", "24",
                   "sleep", "30", conf=True)
        self._tmux("new-window", "-d", "-t", "t:", "sleep", "30")
        assert self._sizes() == ["42x24", "42x24"]
        assert fit_session("t", 64, 32, socket=self.SOCKET) is True
        assert self._sizes() == ["64x32", "64x32"]
        # A window made afterwards (Ctrl-B c, a REMOTE pick) is the new size
        # too, and still pinned.
        self._tmux("new-window", "-d", "-t", "t:", "sleep", "30")
        assert self._sizes() == ["64x32"] * 3
        manual = self._tmux("show-options", "-w", "-t", "t:2", "window-size")
        assert manual.stdout.split()[-1] == "manual"

    def test_no_session_is_left_for_new_session_to_make(self):
        from dsterm.serve import fit_session

        assert fit_session("t", 64, 32, socket=self.SOCKET) is False
        self._tmux("new-session", "-d", "-s", "other", "sleep", "30", conf=True)
        # "=t" is that session exactly, never a prefix of another name.
        self._tmux("rename-session", "-t", "other", "tt")
        assert fit_session("t", 64, 32, socket=self.SOCKET) is False


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestAREMOTEWindowOpensWhatItSays:
    """remote.new_window against a real tmux: what tmux would re-read -- a
    format in the directory, a trailing ';', a one-word command it would hand
    to a shell -- arrives as written."""

    SOCKET = "dstermtest-newwin"

    def _tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.SOCKET, *args],
                              capture_output=True, text=True, timeout=10)

    @pytest.fixture(autouse=True)
    def _clean(self):
        self._tmux("kill-server")
        self._tmux("-f", "/dev/null", "new-session", "-d", "-s", "t", "sleep", "60")
        yield
        self._tmux("kill-server")

    def _path_of(self, window_id):
        return self._tmux("display-message", "-p", "-t", window_id,
                          "#{pane_current_path}").stdout.strip()

    def test_the_directory_and_the_command_are_taken_literally(self, tmp_path):
        import os
        import time

        from dsterm import remote

        odd = tmp_path / "x#Sy;"
        odd.mkdir()
        tools = tmp_path / "My Tools"
        tools.mkdir()
        marker = tmp_path / "ran"
        prog = tools / "prog"
        prog.write_text(f"#!/bin/sh\necho \"$PWD\" > '{marker}'\nsleep 30\n")
        prog.chmod(0o755)
        window = remote.new_window(self.SOCKET, "t", "n", str(odd), [str(prog)])
        assert window is not None
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        # The one-word command with a space in its path ran...
        assert marker.exists(), "a one-word command was re-parsed by a shell"
        # ...in the directory named, not in one a format made up.
        assert os.path.realpath(marker.read_text().strip()) == os.path.realpath(odd)
        assert os.path.realpath(self._path_of(window)) == os.path.realpath(odd)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestCameraPopup:
    """The camera popup against a real tmux: a pty-attached second client
    gets one, and closing it leaves the client list as it was."""

    SOCKET = "dstermtest-campopup"

    def _tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.SOCKET, *args],
                              capture_output=True, text=True, timeout=10)

    def test_open_and_close_on_another_client(self):
        import os
        import pty
        import time

        from dsterm import camera as dc

        ver = self._tmux("-V").stdout.strip()
        try:
            major, minor = (int(p) for p in ver.split()[1]
                            .rstrip("ab").split(".")[:2])
            if (major, minor) < (3, 2):
                pytest.skip(f"tmux {ver} is older than 3.2")
        except (IndexError, ValueError):
            pytest.skip(f"unparseable tmux version {ver!r}")

        self._tmux("kill-server")
        try:
            self._tmux("-f", "/dev/null", "new-session", "-d", "-s", "probe",
                       "-x", "80", "-y", "24", "sleep", "60")
            pid, fd = pty.fork()
            if pid == 0:
                os.environ["TERM"] = "xterm-256color"
                os.execvp("tmux", ["tmux", "-L", self.SOCKET,
                                   "attach", "-t", "probe"])
                os._exit(1)
            time.sleep(1.0)
            clients = self._tmux("list-clients", "-t", "=probe", "-F",
                                 "#{client_pid}\t#{client_name}").stdout
            assert clients.strip(), "the pty client never attached"

            spawned = []

            def starter(argv):
                # Returned, as the server's own starter does: the controller
                # closes a popup only while its spawn is still running.
                proc = subprocess.Popen(argv)
                spawned.append(proc)
                return proc

            ctl = dc.PopupController(
                runner=lambda argv: subprocess.run(
                    argv, capture_output=True, text=True, timeout=10),
                socket_name=self.SOCKET, session_name="=probe",
                own_pid_fn=lambda: -1,
                command_fn=lambda: "sleep 30",
                starter=starter)
            try:
                ctl.reconcile(True)
                deadline = time.monotonic() + 5
                while not spawned and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert spawned, "no popup was opened"
                proc = spawned[0]
                # The popup's display-popup process lives as long as it is up
                # (it blocks on the command); the client list is unchanged.
                time.sleep(0.5)
                assert proc.poll() is None
                ctl._last_reconcile = 0.0
                ctl.reconcile(False)
                deadline = time.monotonic() + 5
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert proc.poll() is not None, "the popup never closed"
                after = self._tmux("list-clients", "-t", "=probe", "-F",
                                   "#{client_pid}\t#{client_name}").stdout
                assert after == clients
            finally:
                ctl.close()
        finally:
            try:
                os.kill(pid, 9)
            except (OSError, UnboundLocalError):
                pass
            self._tmux("kill-server")
