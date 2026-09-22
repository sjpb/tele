# tele

tele — run a command on a remote host, immune to hangups.

## SYNOPSIS

```shell
tele [options] -- user@host command [cmd_options] [cmd_args]
tele [options] -- HostAlias command [cmd_options] [cmd_args]
tele --kill [SIGNAL] -- user@host command [cmd_options] [cmd_args]
```

## DESCRIPTION

`tele` runs `command` on a remote host via SSH, detached from the local session so that it keeps running if tele is killed, the network drops, or the local machine sleeps. By default `tele` blocks, streaming the remote command's stdout/stderr locally, until the command finishes, and then exits with the command's own return code.

If a matching invocation (see IDENTITY below) is already running remotely, tele does not start a second copy. Instead it reattaches: it resumes polling and streaming output from the in-progress run.

If a matching invocation has already completed but its result has not yet been collected by any `tele` client, `tele` reports that stored result (output and exit code) and cleans up, rather than starting a new run.

`tele` requires an explicit `--` between its own options and the remote host/command, since remote `cmd_options` may themselves look like `tele` options and no attempt is made to disambiguate them positionally.

## IDENTITY

Two invocations are considered "the same" if `command`, `cmd_options` and `cmd_args`, as received by `tele` after any local shell expansion, are identical. `ssh_host` is not part of the identity; state lives on the remote host itself, so identity is scoped per-host implicitly.

This identity is used only to detect running or completed instances of the same invocation - it does not prevent side effects from rerunning completed invocations. `tele` will happily run the same command twice in sequence, once any prior result has been observed and cleaned up; it only refuses to start a second concurrent copy, and never silently discards an unreported result.

## ARGUMENTS

`user@host` - SSH target as user@host, optionally with a port via standard SSH syntax, or
`HostAlias` - a Host entry from SSH configuration.
`command` - the remote command to run.
`cmd_options`, `cmd_args` - passed through to command verbatim and unparsed by `tele`.

## OPTIONS

OPTIONS
`--poll SECONDS`
In normal (launch/reattach) mode: interval between checks of remote command status while blocking or reattached. Default: 30.
In --kill mode: interval between checks of whether the signaled process has died. Default: 1.
`--timeout SECONDS`
In normal (launch/reattach) mode: maximum time tele will block waiting locally for the command to complete. Default: 0 (no timeout — block indefinitely). On expiry, tele exits with a distinct return code (see RETURN CODES) and leaves the remote command running, untouched — the same state --async would leave it in (see FUTURE WORK). No cleanup occurs; a later tele invocation with the same identity will reattach or collect the result as normal.
In --kill mode: maximum time to wait for the signaled process to die before giving up. Default: 5. On expiry, the process is left running (no escalation, e.g. to SIGKILL, is attempted) and remote state is left as-is (not cleaned up); tele exits with a distinct return code (see RETURN CODES) so the caller knows the kill did not take effect and may retry, e.g. with a stronger signal. --poll and --timeout share the same names and general purpose (periodic check, give-up bound) in both modes, but their meaning and defaults differ by mode: in launch mode they bound waiting for the remote command, in --kill mode they bound waiting for the signal to take effect.
`--state-path PATH`
Remote path under which lock, status, PID, and output files are stored. Default: under the user's remote home directory.
`--kill [SIGNAL]`
If a matching invocation (per IDENTITY) is currently running, send it SIGNAL (name or number, as accepted by kill(1); default matches kill's own default, SIGTERM), sent to the remote command's process group so that child processes are reached as well. tele then polls (per --poll) for the process to die, up to --timeout:
If the process dies (whether from this signal or otherwise) within the timeout, remote state is cleaned up and tele exits 0 — the invocation is now fully gone and a subsequent tele with the same identity starts fresh.
If the process is still alive when --timeout expires, no cleanup occurs and tele exits with a distinct return code (see RETURN CODES).
If matching state exists but no process is running (already dead, unreported), it is cleaned up directly, no signal needed, exit 0. If no matching state exists at all, this is a silent no-op, exit 0. Repeated tele --kill calls against the same invocation are therefore safe.

# BEHAVIOR ON INVOCATION

Given the computed identity hash, on each invocation `tele` checks remote state:

1. No matching state exists: Start a new run; the remote side records the command, a PID, and time marker to guard against false positives from PID reuse after a reboot, and begins writing stdout/stderr to log files.
2. Matching state exists and the process is confirmed live: Reattach, resuming polling at `--poll` interval, and resume streaming output (see OUTPUT below). Never starts a duplicate run, and no option overrides this.
3. Matching state exists, process is not live, and completion has not yet been reported to any client: Report the stored exit code and output, then clean up remote state.
4. Matching state exists and was already reported by a previous client: Cannot occur under normal single-client use, since state is cleaned up at the point of reporting . Concurrent `tele` clients against the same invocation are not a supported use case, but if `tele` is run concurrently against the same identity and finds state has disappeared mid-poll, it treats this as "completed and already reaped elsewhere," exits, and reports the command's exit status as unknown.

## OUTPUT

Remote stdout/stderr are written to append-only log files on the remote host. Each `tele` client tracks, independently, how much of each log file it has already shown locally (as byte offset), and on each poll reads and displays only new content past that offset. This allows reattachment to resume showing only new output, draining the log like a pipe that only yields unread bytes, without requiring any daemon or client/server protocol; all coordination is file-based.

## CLEANUP

Once a client has observed and reported command completion (success or failure), it deletes the associated remote lock/status/PID/output files. Cleanup is performed by whichever client first observes completion; see BEHAVIOR ON INVOCATION (case 4) for the defined behavior when a second client encounters files removed out from under it.

## PRIVILEGE ESCALATION

Commands requiring interactive privilege elevation (e.g. sudo prompting for a password) are not supported: the remote command runs detached with no attached TTY, so any such prompt will hang or fail. Running a command under sudo only works if the remote host is configured for non-interactive elevation for that command. Even then, `--kill` may not reliably terminate work done as another user, depending on whether signals sent to sudo's process group are forwarded to its child.

## RETURN CODES
On normal completion, the remote command's own exit code is returned unchanged.
Distinct non-negative `tele`-level codes (analogous to ssh's own exit code conventions) are reserved for: inability to reach the host, failure to establish or confirm the remote run, --timeout expiry, --kill with no matching running invocation, and detection of corrupted/unreadable remote state. Exact code assignments TBD.

## SIGNAL HANDLING

Not yet defined. Provisionally, a local `tele` process that receives an interrupt behaves like `--timeout` expiry: it exits without affecting the remote command, which continues running and can be reattached to later.

# FUTURE WORK
- `--async`: return immediately without blocking or streaming; a later invocation with the same identity reattaches. Closely related to `--timeout` (a `--timeout` expiry leaves the same "detached, still running remotely" state that --async would produce from the start), so both share the same underlying reattach/cleanup logic.
- Formal remote dependency requirements (e.g. whether nohup/setsid suffice or something like dtach is required) are not yet specified.
- Full signal-handling semantics.
- Non-interactive privilege elevation support beyond documenting the current limitation.
