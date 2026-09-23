# tele

`tele` — run a command on a remote host, idempotently and immune to hangups.

## SYNOPSIS

Run or reconnect to a shell command:

```shell
tele [options] -- ssh_host command
```

Run or reconnect to a local shell script:
```shell
tele [options] -- ssh_host path
```

Run or reconnect to a directly-executed command (no shell is invoked). The first form requires at least one of cmd_options or cmd_args:
```shell
tele [options] -- ssh_host command [cmd_options] [cmd_args]
tele [options] --exec -- ssh_host command
```

Ensure a command is terminated and clean up:

```shell
tele --kill [SIGNAL] -- ssh_host command [cmd_options] [cmd_args]
```
TODO: is the -- in the right place?

See examples. TODO

## DESCRIPTION

`tele` runs `command` (with options and arguments, if passed) on a remote host `ssh_host` via SSH, detached from the local session so that it keeps running if tele is killed, the network drops, or the local machine sleeps. By default `tele` blocks, streaming the remote command's stdout/stderr locally, until the command finishes, and then exits with the command's own return code.

Rerunning `tele` with a matching `command`, `cmd_options` and `cmd_args` (if relevant) will not by default re-run an invocation if it was successful, i.e. the remote comamnd had a return code of `0`. If the remote command is still running, it will reattach to that command to resume polling and streaming output. If the remote command completed successfully while `tele` was not attached, it will return any unstreamed output and the status code. However if the command failed, rerunning `tele` will retry the command. A successful command can be re-run using the `--force` option.

Caution: If `command` is a bare command not a shell string, with no arguments or options, and is constructed from untrusted input, use the `--exec` option to avoid shell injection attacks.

## ARGUMENTS

`ssh_host` - SSH target as `user@host`, optionally with a port via standard SSH syntax, or a Host entry from SSH configuration.
`command` - The remote command to run, or, in shell mode, a path to a script file to run remotely.
`cmd_options`, `cmd_args` - Passed through to the remote command verbatim and unparsed by `tele`.

Note `--` must be used to separate TODO: WHAT to disambiguate options for `tele` and the remote command.


## OPTIONS

`--exec`  
Run `command` directly rather than using a remote shell, even if `cmd_options` and `cmd_args` are not provided.

`--poll SECONDS`
In normal (launch/reattach) mode: interval between checks of remote command status while blocking or reattached. Default: 30.
In `--kill` mode: interval between checks of whether the signaled process has died. Default: 1.

`--shell [SHELL]`  
Select the remote shell. Default is the remote account's configured login shell. Ignored if `cmd_options` and `cmd_args` are provided, mutually exclusive with `--exec`.

`--timeout [SECONDS]`  
In normal (launch/reattach) mode: maximum time tele will block waiting locally for the command to complete.
- SECONDS < 0: Block indefinitely, no limit. This is the default.
- SECONDS == 0: Return immediately the remote launch succeeds.
- SECONDS > 0: Block up to that many seconds. On expiry, tele exits with a distinct return code (see RETURN CODES) and leaves the remote command running, with no clean up. A later tele invocation with the same identity will reattach or collect the result as normal.

In `--kill` mode: maximum time to wait for the signaled process to die before giving up, with the same <0 / 0 / >0 meanings as above. Default: 5. On expiry, tele exits with a distinct return code (see RETURN CODES) so the caller knows the kill did not take effect and may retry, e.g. with a stronger signal.

`--force`
Ignore any previous successful run and reun the command. Note if a matching invocation is running, this has no effect - tele always reattaches.

`--state-path PATH`
Remote path under which remote state and output files are stored. Default `$TMPDIR/$USER/tele`. Note `tele` does not clean up state for successful invocations.

`--kill [SIGNAL]`
Ensure no matching invocation (as per IDENTITY) is running, and clean up remote state. SIGNAL is a name or number as accepted by kill(1); default is SIGTERM, matching kill(1)'s own default. See BEHAVIOR IN KILL MODE for the full behaviour.

## IDENTITY

Two invocations are considered "the same" if `command`, `cmd_options` and `cmd_args`, as received by `tele` after any local shell expansion, are identical. `ssh_host` is not part of the identity; state lives on the remote host itself, so identity is scoped per-host implicitly.

## BEHAVIOR ON INVOCATION

Given the identity of the requested invocation, on each invocation `tele` checks remote state:

1. **No matching state exists.** Start a new run. The remote side records the command, a PID, and a time marker to guard against false positives from PID reuse after a reboot, and begins writing stdout/stderr to log files.
2. **Matching state exists and the process is confirmed live.** Reattach, resuming polling at `--poll` interval and resuming output streaming (see OUTPUT below). Never starts a duplicate run, and no option overrides this.
3. **Matching state exists, process is not live, and it exited successfully (0), and `--force` was not given.** Report the retained result (see OUTPUT) without rerunning. State is left in place for future invocations to observe the same way.
4. **Matching state exists, process is not live, and it exited successfully (0), and `--force` was given.** Discard the retained result and start a new run, as in case 1.
5. **Matching state exists, process is not live, and it exited with a failure (non-zero).** Report the stored exit code and output, then clean up remote state. The next matching invocation, with or without `--force`, behaves as case 1.
6. **Matching state existed but was cleaned up by another client between an earlier check and now** (only possible for non-retained, i.e. failure, outcomes - a retained success is never removed by an observing client). Treated as "completed and already reaped elsewhere"; `tele` exits and reports the command's exit status as unknown. Concurrent `tele` clients against the same invocation are not a supported use case, but this behavior is defined rather than left as a race. TODO: how is it possible to detect this?!

## BEHAVIOR IN KILL MODE

Given the identity of the requested invocation, `tele --kill` checks remote state:

1. **No matching state exists.** Nothing to do. Silent no-op, exit `0`.
2. **Matching state exists, process is not live, and it exited successfully (0).** Nothing running, and a retained success is not `--kill`'s to discard — use `--force` on a normal invocation for that instead. No-op, exit `0`.
3. **Matching state exists, process is not live, and it exited with a failure (non-zero), unreported.** No signal needed. Remote state is cleaned up directly, exit `0`.
TODO: Can we simplify all the above to "No matching process is live: Clean up any remote state and exit `0`.
4. **Matching state exists and the process is confirmed live.** `SIGNAL` is sent to the remote command's process group (not just the recorded PID, so child processes are reached too). `tele` then polls for the process to die, at `--poll` interval, up to `--timeout`:
   - **Dies within the timeout** (whether from this signal or otherwise) → remote state is cleaned up (discarding any result, since a killed run is never treated as a retained success), exit `0`. The invocation is now fully gone; a subsequent `tele` with the same identity starts fresh.
   - **Still alive when `--timeout` expires** → no cleanup occurs, no escalation is attempted (e.g. to `SIGKILL`), and `tele` exits with a distinct return code (see RETURN CODES) so the caller knows the kill did not take effect and may retry, e.g. with a stronger signal.

## OUTPUT

Remote stdout/stderr are written to append-only log files on the remote host. Each tele client tracks, independently, how much of each log file it has already shown locally (as a byte offset), and on each poll reads and displays only new content past that offset — draining the log like a pipe that only yields unread bytes, with no daemon or client/server protocol required; all coordination is file-based. On any client draining output logs to EOF, the remote logs are deleted.

When reporting a retained successful completion (BEHAVIOR ON INVOCATION, case 3), `tele` instead prints a short status summary.

## PRIVILEGE ESCALATION

Commands requiring interactive privilege elevation (e.g. sudo prompting for a password) are not supported: the remote command runs detached with no attached TTY, so any such prompt will hang or fail. Running a command under sudo only works if the remote host is configured for non-interactive elevation for that command. Even then, `--kill` may not reliably terminate work done as another user, depending on whether signals sent to sudo's process group are forwarded to its child.

## RETURN CODES

On normal completion, the remote command's own exit code is returned unchanged.

Distinct non-negative tele-level codes (analogous to ssh's own exit code conventions) are reserved for:
- Inability to reach the host (SSH-level failure - always reported, regardless of --timeout), failure to establish the remote run itself (e.g. state directory unwritable, remote script rejected — always confirmed and reported before returning, even under --timeout 0)
- `--timeout N` expiry in normal mode
- `--timeout N` expiry in kill mode
- `--timeout 0` returning by design with no result yet available ("launched/reattached, not waited"). This is distinct from timeout expiry
- Detection of corrupted/unreadable remote state.

## SIGNAL HANDLING

Not yet defined. Provisionally, a local `tele` process that receives an interrupt behaves like `--timeout` expiry: it exits without affecting the remote command, which continues running and can be reattached to later.

## FUTURE WORK

- A `tele`-native way to list and prune accumulated state under a custom `--state-path`.
- Formal remote dependency requirements (e.g. whether nohup/setsid suffice or something like dtach is required) are not yet specified.
- Full signal-handling semantics.
- Non-interactive privilege elevation support beyond documenting the current limitation.
