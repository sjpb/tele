#!/usr/bin/env python3
"""Tests for tele.py.

No real SSH server is needed. A shim `ssh` (written to a temp dir) drops the
ssh options and host and runs the "remote" command locally, so the whole
client <-> remote-agent protocol is exercised for real; only the network hop is
faked. Each test gets its own state directory.

Run:   python3 test_tele.py -v          (or: python3 -m unittest -v test_tele)
Env:   TELE_PATH=/path/to/tele.py       to test a copy elsewhere

Shim knobs (env vars read by the shim):
    FAKE_REMOTE_SH    program used as the remote `sh`      (default: sh)
    FAKE_REMOTE_PATH  PATH on the "remote"                 (default: unchanged)
    FAKE_BANNER       text printed to stdout before output (login-banner noise)
    FAKE_SSH_FAIL     if set, behave like an unreachable host (exit 255)
"""
import glob
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

TELE = os.environ.get("TELE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tele.py"))
HOST = "testhost"

# Return codes (must match tele.py)
EX_USAGE, EX_BAD_STATE, EX_LAUNCHED, EX_KILL_TIMEOUT = 2, 250, 251, 252
EX_TIMEOUT, EX_REMOTE, EX_SSH = 253, 254, 255

FAKESSH = r"""#!/bin/sh
[ -n "$FAKE_SSH_FAIL" ] && { echo "ssh: Could not resolve hostname $FAKE_SSH_FAIL" >&2; exit 255; }
[ -n "$FAKE_BANNER" ] && echo "$FAKE_BANNER"
while [ $# -gt 0 ]; do case "$1" in --) shift; break;; -o) shift 2;; -*) shift;; *) break;; esac; done
shift   # host
[ -n "$FAKE_REMOTE_PATH" ] && PATH="$FAKE_REMOTE_PATH"
exec ${FAKE_REMOTE_SH:-sh} -c "$*"
"""

_shimdir = None


def setUpModule():
    global _shimdir
    _shimdir = tempfile.mkdtemp(prefix="tele-shim-")
    path = os.path.join(_shimdir, "fakessh")
    with open(path, "w") as f:
        f.write(FAKESSH)
    os.chmod(path, 0o755)


def tearDownModule():
    shutil.rmtree(_shimdir, ignore_errors=True)


class TeleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tele-test-")
        self.state = os.path.join(self.tmp, "state")
        self.env = dict(os.environ, TELE_SSH=os.path.join(_shimdir, "fakessh"))

    def tearDown(self):
        self._reap()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def argv(self, *args):
        return [sys.executable, TELE, "--state-path", self.state, *args]

    def tele(self, *args, env=None, timeout=60):
        """Run tele to completion; returns CompletedProcess (bytes)."""
        return subprocess.run(self.argv(*args), capture_output=True, timeout=timeout,
                              env={**self.env, **(env or {})})

    def out(self, p):
        return p.stdout.decode()

    def err(self, p):
        return p.stderr.decode()

    def state_dirs(self):
        return sorted(glob.glob(os.path.join(self.state, "*")))

    def counter(self):
        """Lines in the side-effect file that test commands append to."""
        try:
            with open(os.path.join(self.tmp, "count")) as f:
                return len(f.read().splitlines())
        except FileNotFoundError:
            return 0

    def count_cmd(self, extra=""):
        return f"echo x >> {self.tmp}/count; {extra}"

    def proc_running(self, cmdline):
        """Is a process with exactly this command line alive? (exact match, so
        wrapper shells that merely contain the text don't count)"""
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        return any(line.strip() == cmdline for line in ps.splitlines())

    def _reap(self):
        for pidfile in glob.glob(os.path.join(self.state, "*", "pid")):
            try:
                with open(pidfile) as fh:
                    pid = int(fh.read().strip())
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
class TestBasicRuns(TeleTestCase):
    def test_shell_mode_streams_stdout_and_stderr_and_exits_0(self):
        p = self.tele("--poll", "1", HOST, "--", "echo out1; sleep 1; echo err1 >&2; echo out2")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.out(p), "out1\nout2\n")
        self.assertEqual(self.err(p), "err1\n")

    def test_exit_code_is_passed_through_unchanged(self):
        for code in (1, 3, 42, 130):
            with self.subTest(code=code):
                self.assertEqual(self.tele(HOST, "--", f"exit {code}").returncode, code)

    def test_successful_run_is_not_rerun_and_prints_summary(self):
        cmd = self.count_cmd("echo hello")
        p1 = self.tele(HOST, "--", cmd)
        self.assertEqual((p1.returncode, self.out(p1)), (0, "hello\n"))
        p2 = self.tele(HOST, "--", cmd)
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(self.out(p2), "")                       # output already delivered
        self.assertIn("already completed successfully", self.err(p2))
        self.assertEqual(self.counter(), 1)                      # NOT re-run

    def test_force_reruns_a_successful_command(self):
        cmd = self.count_cmd()
        self.tele(HOST, "--", cmd)
        p = self.tele("--force", HOST, "--", cmd)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.counter(), 2)

    def test_failed_run_is_retried_next_time_and_state_is_cleaned(self):
        cmd = self.count_cmd("echo boom >&2; exit 3")
        for n in (1, 2):
            p = self.tele(HOST, "--", cmd)
            self.assertEqual(p.returncode, 3)
            self.assertEqual(self.err(p), "boom\n")
            self.assertEqual(self.counter(), n)                  # ran again each time
            self.assertEqual(self.state_dirs(), [])              # failure state cleaned up

    def test_success_keeps_state_but_deletes_drained_logs(self):
        self.tele(HOST, "--", "echo hi")
        (d,) = self.state_dirs()
        names = set(os.listdir(d))
        self.assertIn("exit", names)
        self.assertIn("cmdline", names)
        self.assertNotIn("stdout.log", names)
        self.assertNotIn("stderr.log", names)

    def test_identity_is_per_command(self):
        self.tele(HOST, "--", "echo a")
        self.tele(HOST, "--", "echo b")
        self.assertEqual(len(self.state_dirs()), 2)


class TestModes(TeleTestCase):
    def test_exec_mode_runs_without_a_shell(self):
        p = self.tele(HOST, "--", "printf", "%s|", "a b", "$HOME;x", "`id`")
        self.assertEqual(self.out(p), "a b|$HOME;x|`id`|")       # metacharacters stay literal
        self.assertEqual(p.returncode, 0)

    def test_exec_flag_with_single_word(self):
        p = self.tele("--exec", HOST, "--", "true")
        self.assertEqual(p.returncode, 0)
        p = self.tele("--exec", HOST, "--", "false")
        self.assertEqual(p.returncode, 1)

    def test_exec_flag_does_not_interpret_shell_syntax(self):
        p = self.tele("--exec", HOST, "--", "echo hi; echo injected")
        self.assertEqual(p.returncode, 127)                      # no such command
        self.assertNotIn("injected", self.out(p))

    def test_script_mode_uploads_and_runs_local_script(self):
        script = os.path.join(self.tmp, "s.sh")
        with open(script, "w") as f:
            f.write('echo "arg0=$(basename "$0")"; echo from-script; exit 5\n')
        p = self.tele(HOST, "--", script)
        self.assertEqual(p.returncode, 5)
        self.assertIn("from-script", self.out(p))

    def test_script_runs_even_if_file_is_gone_when_reattaching_or_killing(self):
        script = os.path.join(self.tmp, "s.sh")
        with open(script, "w") as f:
            f.write("sleep 3151\n")
        self.assertEqual(self.tele("--timeout", "0", HOST, "--", script).returncode, EX_LAUNCHED)
        os.remove(script)
        self.assertEqual(self.tele("--kill", HOST, "--", script).returncode, 0)
        self.assertFalse(self.proc_running("sleep 3151"))

    def test_shell_option_with_arguments(self):
        p = self.tele("--shell", "bash -e", HOST, "--", "echo $BASH_VERSION | cut -c1")
        self.assertEqual(p.returncode, 0)
        self.assertRegex(self.out(p), r"^\d")

    def test_shell_mode_can_use_shell_syntax(self):
        p = self.tele(HOST, "--", "echo a | tr a b; echo $((1+2))")
        self.assertEqual(self.out(p), "b\n3\n")

    def test_command_runs_in_callers_umask_not_teles_private_umask(self):
        f = os.path.join(self.tmp, "made")
        p = self.tele(HOST, "--", f"umask; touch {f}")
        caller = subprocess.run("umask", shell=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.out(p).strip().lstrip("0"), caller.lstrip("0"))
        self.assertNotEqual(oct(os.stat(f).st_mode & 0o777), "0o600")

    def test_state_files_are_private(self):
        self.tele(HOST, "--", "echo hi")
        (d,) = self.state_dirs()
        self.assertEqual(os.stat(d).st_mode & 0o077, 0)
        self.assertEqual(os.stat(os.path.join(d, "exit")).st_mode & 0o077, 0)


class TestDetachAndReattach(TeleTestCase):
    TICKS = "for i in 1 2 3 4; do echo tick $i; sleep 1; done"
    ALL = "".join(f"tick {i}\n" for i in range(1, 5))

    def test_timeout_0_launches_and_returns_immediately(self):
        t = time.time()
        p = self.tele("--timeout", "0", HOST, "--", "sleep 5; echo done")
        self.assertEqual(p.returncode, EX_LAUNCHED)
        self.assertLess(time.time() - t, 4)
        self.assertIn("not waiting", self.err(p))

    def test_reattach_delivers_every_line_exactly_once(self):
        outs = []
        p = self.tele("--timeout", "0", HOST, "--", self.TICKS)
        self.assertEqual(p.returncode, EX_LAUNCHED)
        outs.append(self.out(p))
        time.sleep(1.5)
        p = self.tele("--timeout", "1", "--poll", "1", HOST, "--", self.TICKS)
        self.assertEqual(p.returncode, EX_TIMEOUT)
        self.assertIn("reattached", self.err(p))
        outs.append(self.out(p))
        p = self.tele("--poll", "1", HOST, "--", self.TICKS)
        self.assertEqual(p.returncode, 0)
        outs.append(self.out(p))
        self.assertEqual("".join(outs), self.ALL)                # no loss, no duplicates

    def test_timeout_expiry_leaves_command_running(self):
        p = self.tele("--timeout", "1", "--poll", "1", HOST, "--", "sleep 3152")
        self.assertEqual(p.returncode, EX_TIMEOUT)
        self.assertTrue(self.proc_running("sleep 3152"))
        self.assertEqual(self.tele("--kill", HOST, "--", "sleep 3152").returncode, 0)

    def test_never_starts_a_duplicate_while_running(self):
        cmd = self.count_cmd("sleep 3")
        self.tele("--timeout", "0", HOST, "--", cmd)
        self.tele("--timeout", "0", HOST, "--", cmd)
        self.tele("--timeout", "0", "--force", HOST, "--", cmd)  # --force can't override a live run
        self.assertEqual(self.counter(), 1)

    def test_success_while_unattached_returns_unstreamed_output_then_summary(self):
        cmd = "sleep 1; echo late-output"
        self.assertEqual(self.tele("--timeout", "0", HOST, "--", cmd).returncode, EX_LAUNCHED)
        time.sleep(2.5)
        p = self.tele(HOST, "--", cmd)
        self.assertEqual((p.returncode, self.out(p)), (0, "late-output\n"))
        self.assertIn("already completed successfully", self.err(p))
        p = self.tele(HOST, "--", cmd)                           # now drained: summary only
        self.assertEqual((p.returncode, self.out(p)), (0, ""))

    def test_failure_while_unattached_is_reported_once_then_next_run_is_fresh(self):
        cmd = self.count_cmd("sleep 1; echo bad >&2; exit 7")
        self.assertEqual(self.tele("--timeout", "0", HOST, "--", cmd).returncode, EX_LAUNCHED)
        time.sleep(2.5)
        p = self.tele(HOST, "--", cmd)                           # reports stored result
        self.assertEqual(p.returncode, 7)
        self.assertEqual(self.err(p).count("bad\n"), 1)
        self.assertIn("previous run failed", self.err(p))
        self.assertEqual(self.counter(), 1)                      # did NOT re-run
        self.assertEqual(self.state_dirs(), [])
        p = self.tele(HOST, "--", cmd)                           # now a fresh run
        self.assertEqual(p.returncode, 7)
        self.assertEqual(self.counter(), 2)

    def test_ctrl_c_leaves_remote_running_and_reattach_gets_the_rest(self):
        cmd = "for i in 1 2 3 4 5 6; do echo t$i; sleep 1; done"
        proc = subprocess.Popen(self.argv("--poll", "1", HOST, "--", cmd),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        time.sleep(2.5)
        proc.send_signal(signal.SIGINT)
        o1, e1 = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, EX_TIMEOUT)
        self.assertIn(b"interrupted", e1)
        p = self.tele("--poll", "1", HOST, "--", cmd)
        self.assertEqual(p.returncode, 0)
        self.assertEqual((o1 + p.stdout).decode(), "".join(f"t{i}\n" for i in range(1, 7)))

    def test_survives_sigterm_of_local_process_too(self):
        proc = subprocess.Popen(self.argv("--poll", "1", HOST, "--", "sleep 3153"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        time.sleep(2)
        proc.terminate()
        proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, EX_TIMEOUT)
        self.assertTrue(self.proc_running("sleep 3153"))


class TestKill(TeleTestCase):
    def test_kill_reaps_whole_process_group_and_cleans_state(self):
        cmd = "sleep 3141 & sleep 3142 & wait"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(0.7)
        self.assertTrue(self.proc_running("sleep 3141") and self.proc_running("sleep 3142"))
        p = self.tele("--kill", HOST, "--", cmd)
        self.assertEqual(p.returncode, 0)
        time.sleep(0.3)
        self.assertFalse(self.proc_running("sleep 3141") or self.proc_running("sleep 3142"))
        self.assertEqual(self.state_dirs(), [])
        # ...and a later identical invocation starts fresh
        p = self.tele("--timeout", "0", HOST, "--", cmd)
        self.assertEqual(p.returncode, EX_LAUNCHED)
        self.tele("--kill", HOST, "--", cmd)

    def test_kill_with_nothing_to_kill_is_silent_noop(self):
        p = self.tele("--kill", HOST, "--", "sleep 3154")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))

    def test_kill_does_not_discard_a_retained_success(self):
        cmd = self.count_cmd()
        self.tele(HOST, "--", cmd)
        self.assertEqual(self.tele("--kill", HOST, "--", cmd).returncode, 0)
        p = self.tele(HOST, "--", cmd)
        self.assertIn("already completed", self.err(p))
        self.assertEqual(self.counter(), 1)

    def test_kill_cleans_up_unreported_failure(self):
        cmd = "sleep 1; exit 4"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(2)
        self.assertEqual(self.tele("--kill", HOST, "--", cmd).returncode, 0)
        self.assertEqual(self.state_dirs(), [])

    def test_kill_when_signal_is_ignored_returns_distinct_code_then_kill9_works(self):
        cmd = "trap '' TERM; sleep 3155"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(0.7)
        p = self.tele("--kill", "--timeout", "2", HOST, "--", cmd)
        self.assertEqual(p.returncode, EX_KILL_TIMEOUT)
        self.assertNotEqual(self.state_dirs(), [])               # no cleanup on expiry
        p = self.tele("--kill", "--signal", "KILL", HOST, "--", cmd)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.state_dirs(), [])

    def test_kill_timeout_0_signals_once_and_reports_unconfirmed(self):
        cmd = "trap '' TERM; sleep 3156"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(0.7)
        self.assertEqual(self.tele("--kill", "--timeout", "0", HOST, "--", cmd).returncode, EX_KILL_TIMEOUT)
        self.tele("--kill", "--signal", "KILL", HOST, "--", cmd)

    def test_kill_signal_syntaxes(self):
        for spec in (["--kill", "--signal", "9"], ["--kill", "--signal", "SIGKILL"],
                     ["--kill", "--signal=KILL"], ["--kill", "--signal", "kill"],
                     ["--kill", "--signal", "KILL"]):
            with self.subTest(spec=spec):
                cmd = "sleep 3157"
                self.tele("--timeout", "0", HOST, "--", cmd)
                time.sleep(0.5)
                self.assertEqual(self.tele(*spec, HOST, "--", cmd).returncode, 0)
                self.assertFalse(self.proc_running("sleep 3157"))

    def test_bare_kill_does_not_swallow_the_host(self):
        cmd = "sleep 3158"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(0.5)
        self.assertEqual(self.tele("--kill", HOST, "--", cmd).returncode, 0)
        self.assertFalse(self.proc_running("sleep 3158"))

    def test_readmes_kill_synopsis_with_host_after_double_dash(self):
        # tele --kill [--signal SIGNAL] -- ssh_host command...
        cmd = "sleep 3159"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(0.5)
        self.assertEqual(self.tele("--kill", "--signal", "TERM", "--", HOST, cmd).returncode, 0)
        self.assertFalse(self.proc_running("sleep 3159"))

    def test_runner_records_status_of_a_command_killed_by_signal(self):
        # A signal to the process group should end the command, not orphan the run:
        cmd = "sleep 3160"
        self.tele("--timeout", "0", HOST, "--", cmd)
        time.sleep(0.5)
        (d,) = self.state_dirs()
        pgid = int(open(os.path.join(d, "pid")).read())
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(1)
        p = self.tele(HOST, "--", cmd)
        self.assertEqual(p.returncode, 143)                      # 128+SIGTERM, recorded by the runner


class TestOutputHandling(TeleTestCase):
    def test_large_output_is_chunked_without_corruption(self):
        n = 2_500_000                                            # > 2 chunks of 1 MiB
        p = self.tele(HOST, "--", f"yes 0123456789abcdef | head -c {n}")
        expect = hashlib.md5((b"0123456789abcdef\n" * (n // 17 + 1))[:n]).hexdigest()
        self.assertEqual(hashlib.md5(p.stdout).hexdigest(), expect)
        self.assertEqual(len(p.stdout), n)

    def test_binary_output_all_byte_values_round_trip(self):
        cmd = "i=0; while [ $i -lt 256 ]; do printf \"\\\\$(printf %03o $i)\"; i=$((i+1)); done"
        p = self.tele("--shell", "bash", HOST, "--", cmd)
        self.assertEqual(p.stdout, bytes(range(256)))

    def test_stdout_and_stderr_are_kept_separate(self):
        p = self.tele(HOST, "--", "echo to-out; echo to-err >&2")
        self.assertEqual(self.out(p), "to-out\n")
        self.assertEqual(self.err(p), "to-err\n")

    def test_output_without_trailing_newline(self):
        p = self.tele(HOST, "--", "printf abc")
        self.assertEqual(p.stdout, b"abc")

    def test_login_banner_noise_on_stdout_is_ignored(self):
        p = self.tele(HOST, "--", "echo real", env={"FAKE_BANNER": "Welcome to the host!"})
        self.assertEqual(self.out(p), "real\n")

    def test_remote_command_cannot_read_tele_protocol_stdin(self):
        p = self.tele(HOST, "--", "cat; echo after-cat")        # stdin is /dev/null, so cat returns at once
        self.assertEqual((p.returncode, self.out(p)), (0, "after-cat\n"))


class TestRemoteEnvironments(TeleTestCase):
    def test_works_when_remote_sh_is_bash_posix(self):
        p = self.tele(HOST, "--", "echo from-bash", env={"FAKE_REMOTE_SH": "bash --posix"})
        self.assertEqual((p.returncode, self.out(p)), (0, "from-bash\n"))

    def test_setsid_missing_falls_back_and_group_kill_still_works(self):
        if not shutil.which("perl"):
            self.skipTest("perl not available for the fallback")
        bindir = os.path.join(self.tmp, "nosetsid")
        os.mkdir(bindir)
        for d in ("/usr/bin", "/bin"):
            for f in os.listdir(d):
                if f != "setsid" and not os.path.lexists(os.path.join(bindir, f)):
                    os.symlink(os.path.join(d, f), os.path.join(bindir, f))
        env = {"FAKE_REMOTE_PATH": bindir}
        cmd = "sleep 3161 & sleep 3162 & wait"
        self.assertEqual(self.tele("--timeout", "0", HOST, "--", cmd, env=env).returncode, EX_LAUNCHED)
        time.sleep(0.7)
        (d,) = self.state_dirs()
        pid = int(open(os.path.join(d, "pid")).read())
        self.assertEqual(os.getpgid(pid), pid)                   # runner is its own group leader
        self.assertEqual(self.tele("--kill", HOST, "--", cmd, env=env).returncode, 0)
        time.sleep(0.3)
        self.assertFalse(self.proc_running("sleep 3161") or self.proc_running("sleep 3162"))

    def test_default_state_path_uses_tmpdir_and_user(self):
        tmpdir = os.path.join(self.tmp, "tmpdir")
        os.mkdir(tmpdir)
        cmd = [sys.executable, TELE, HOST, "--", "echo hi"]
        p = subprocess.run(cmd, capture_output=True, env={**self.env, "TMPDIR": tmpdir, "USER": "someone"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(glob.glob(os.path.join(tmpdir, "someone", "tele", "*"))), 1)

    def test_state_path_tilde_expands_on_remote(self):
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        cmd = [sys.executable, TELE, "--state-path", "~/tele-state", HOST, "--", "echo hi"]
        p = subprocess.run(cmd, capture_output=True, env={**self.env, "HOME": home})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(glob.glob(os.path.join(home, "tele-state", "*"))), 1)

    def test_refuses_state_dir_not_owned_by_user(self):
        if os.geteuid() == 0:
            self.skipTest("root owns everything it creates")
        p = self.tele("--state-path", "/", HOST, "--", "echo hi")
        self.assertEqual(p.returncode, EX_REMOTE)
        self.assertIn("not owned", self.err(p))


class TestFailureModes(TeleTestCase):
    def test_unreachable_host_returns_255_regardless_of_timeout(self):
        for extra in ([], ["--timeout", "0"]):
            with self.subTest(extra=extra):
                p = self.tele(*extra, HOST, "--", "echo x", env={"FAKE_SSH_FAIL": HOST})
                self.assertEqual(p.returncode, EX_SSH)
                self.assertIn("Could not resolve", self.err(p))

    def test_missing_ssh_binary_returns_255(self):
        p = self.tele(HOST, "--", "echo x", env={"TELE_SSH": "/nonexistent/ssh"})
        self.assertEqual(p.returncode, EX_SSH)

    def test_unwritable_state_dir_is_reported_even_with_timeout_0(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        p = self.tele("--timeout", "0", "--state-path", "/proc/nope/tele", HOST, "--", "echo x")
        self.assertEqual(p.returncode, EX_REMOTE)
        self.assertIn("cannot create state directory", self.err(p))

    def test_state_vanishing_while_attached_reports_unknown_status(self):
        # README case 6
        import threading
        threading.Timer(1.5, lambda: shutil.rmtree(self.state, ignore_errors=True)).start()
        try:
            p = self.tele("--poll", "1", HOST, "--", "sleep 3165; echo never")
            self.assertEqual(p.returncode, EX_BAD_STATE)
            self.assertIn("disappeared", self.err(p))
        finally:
            subprocess.run(["pkill", "-f", "^sleep 3165$"], capture_output=True)

    def test_pid_reuse_guard_forged_marker_is_not_treated_as_live(self):
        self.tele("--timeout", "0", HOST, "--", "sleep 3163")
        time.sleep(0.5)
        (d,) = self.state_dirs()
        with open(os.path.join(d, "marker"), "w") as f:
            f.write("some-other-process-started-at-another-time\n")
        p = self.tele("--timeout", "0", HOST, "--", "sleep 3163")
        self.assertEqual(p.returncode, EX_BAD_STATE)
        self.assertIn("without recording an exit status", self.err(p))
        self.assertEqual(self.state_dirs(), [])                  # cleaned, next run is fresh
        subprocess.run(["pkill", "-f", "^sleep 3163$"], capture_output=True)

    def test_runner_killed_hard_without_exit_file_is_reported_as_died(self):
        self.tele("--timeout", "0", HOST, "--", "sleep 3164")
        time.sleep(0.5)
        (d,) = self.state_dirs()
        os.killpg(int(open(os.path.join(d, "pid")).read()), signal.SIGKILL)   # runner too: no exit file
        time.sleep(0.3)
        p = self.tele("--timeout", "0", HOST, "--", "sleep 3164")
        self.assertEqual(p.returncode, EX_BAD_STATE)
        self.assertIn("without recording an exit status", self.err(p))

    def test_corrupt_state_is_detected_and_can_be_forced_or_killed_away(self):
        cmd = self.count_cmd("echo a")
        self.tele(HOST, "--", cmd)
        (d,) = self.state_dirs()
        open(os.path.join(d, "exit"), "w").write("garbage\n")
        p = self.tele(HOST, "--", cmd)
        self.assertEqual(p.returncode, EX_BAD_STATE)
        self.assertIn("corrupt", self.err(p))
        self.assertEqual(self.counter(), 1)                      # not silently re-run
        self.assertEqual(self.tele("--force", HOST, "--", cmd).returncode, 0)
        self.assertEqual(self.counter(), 2)
        open(os.path.join(self.state_dirs()[0], "exit"), "w").write("garbage\n")
        self.assertEqual(self.tele("--kill", HOST, "--", cmd).returncode, 0)
        self.assertEqual(self.state_dirs(), [])

    def test_missing_runner_file_counts_as_corrupt(self):
        cmd = "echo a"
        self.tele(HOST, "--", cmd)
        (d,) = self.state_dirs()
        os.remove(os.path.join(d, "run.sh"))
        self.assertEqual(self.tele(HOST, "--", cmd).returncode, EX_BAD_STATE)

    def test_garbage_reply_from_remote_is_reported_as_remote_error(self):
        bad = os.path.join(self.tmp, "garbage_ssh")
        with open(bad, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null; echo 'this is not a tele reply'\n")
        os.chmod(bad, 0o755)
        p = self.tele(HOST, "--", "echo x", env={"TELE_SSH": bad})
        self.assertEqual(p.returncode, EX_REMOTE)
        self.assertIn("unexpected reply", self.err(p))

    def test_truncated_reply_is_an_error_not_silent_data_loss(self):
        trunc = os.path.join(self.tmp, "trunc_ssh")
        real = os.path.join(_shimdir, "fakessh")
        with open(trunc, "w") as f:                              # cut the reply off mid-data
            f.write(f"#!/bin/sh\n{real} \"$@\" | head -c 60\n")
        os.chmod(trunc, 0o755)
        p = self.tele(HOST, "--", "echo x", env={"TELE_SSH": trunc})
        self.assertEqual(p.returncode, EX_REMOTE)


class TestCommandLine(TeleTestCase):
    def assertUsageError(self, *args, msg=None):
        p = self.tele(*args)
        self.assertEqual(p.returncode, EX_USAGE, self.err(p))
        if msg:
            self.assertIn(msg, self.err(p))

    def test_missing_double_dash(self):
        self.assertUsageError(HOST, "echo hi", msg="missing `--`")

    def test_missing_host(self):
        self.assertUsageError("--", "echo hi", msg="ssh_host is required")

    def test_missing_command(self):
        self.assertUsageError(HOST, "--", msg="no command")
        self.assertUsageError(HOST, "--", "", msg="no command")

    def test_shell_and_exec_are_mutually_exclusive(self):
        self.assertUsageError("--exec", "--shell", "bash", HOST, "--", "x", msg="mutually exclusive")

    def test_options_invalid_with_kill(self):
        for opt in (["--force"], ["--exec"], ["--shell", "bash"]):
            with self.subTest(opt=opt):
                self.assertUsageError("--kill", *opt, HOST, "--", "x")

    def test_bad_signal(self):
        self.assertUsageError("--kill", "--signal", "BOGUS", HOST, "--", "x", msg="unknown signal")

    def test_signal_without_kill_is_rejected(self):
        self.assertUsageError("--signal", "KILL", HOST, "--", "x", msg="only valid with --kill")

    def test_bad_poll(self):
        self.assertUsageError("--poll", "0", HOST, "--", "x")

    def test_host_cannot_look_like_an_ssh_option(self):
        self.assertUsageError("--kill", "--", "-oProxyCommand=evil", "x", msg="invalid ssh_host")
        p = self.tele("-oProxyCommand=evil", "--", "x")
        self.assertNotEqual(p.returncode, 0)

    def test_negative_timeout_value_parses_as_a_value(self):
        p = self.tele("--timeout", "-1", HOST, "--", "echo neg-ok")
        self.assertEqual((p.returncode, self.out(p)), (0, "neg-ok\n"))

    def test_abbreviated_options_are_rejected(self):
        self.assertNotEqual(self.tele("--forc", HOST, "--", "x").returncode, 0)

    def test_help_works_without_double_dash(self):
        p = self.tele("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("usage: tele", self.out(p))

    def test_command_after_dashdash_is_never_parsed_as_tele_options(self):
        p = self.tele(HOST, "--", "printf", "%s\n", "--force", "--kill", "--timeout")
        self.assertEqual(self.out(p), "--force\n--kill\n--timeout\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)