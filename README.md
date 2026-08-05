# claude-followup

Schedule follow-up prompts into a running [Claude Code](https://claude.com/claude-code) session.

You hit your usage limit at 11pm. Instead of setting an alarm, you queue the
next prompt and go to bed — when the limit resets, the message gets typed into
your session and Claude picks up where it left off.

```console
$ claude-followup schedule claude4 --auto -m "continue the refactor"
queued -> zellij:claude4
  fires  2026-08-06 03:00:30 UTC  (in 4h01m, includes +30s buffer)
  via    auto: reset 3am (UTC) from transcript
  says   continue the refactor
  unit   claude-followup-claude4-1785985230-a41c.timer
  cancel claude-followup cancel claude4
```

It types into a **zellij** or **tmux** session, and hands delivery to a
transient **systemd `--user`** timer — so it fires whether or not your terminal
is attached, and whether or not this process is still around.

## Install

```sh
uv tool install claude-followup
# or
pipx install claude-followup
# or just drop the single file on your PATH — it has no dependencies
curl -o ~/.local/bin/claude-followup https://raw.githubusercontent.com/poruchik-bi/claude-followup/main/claude_followup.py
chmod +x ~/.local/bin/claude-followup
```

Install straight from the repo:

```sh
uv tool install git+https://github.com/poruchik-bi/claude-followup
# or
pipx install git+https://github.com/poruchik-bi/claude-followup
```

**Requirements:** Linux with systemd user sessions, Python 3.9+, and zellij
and/or tmux. No third-party Python packages.

### On a headless or SSH-only box, enable lingering

systemd stops your user manager when your last login session ends, which kills
pending timers with it. If you schedule a follow-up over SSH and then log out,
it will not fire unless lingering is on:

```sh
loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger   # want: Linger=yes
```

Desktop sessions usually have this already. Check it once per machine —
everything else works with no setup.

## Usage

```
claude-followup schedule <session> [--in D | --at T | --auto] [-m MESSAGE]
claude-followup send     <session> [-m MESSAGE]     # right now
claude-followup detect   <session>                  # show reset, don't schedule
claude-followup list                                # what's queued
claude-followup cancel   <session|unit> | --all
claude-followup sessions                            # zellij + tmux sessions
```

### Timing

| Form | Accepts |
|---|---|
| `--in DURATION` | `90m`, `45s`, `2h30m`, `1h5m10s`, `"90 minutes"`, `"1 hour and 30 mins"` |
| `--at TIME` | `15:00`, `3:15pm`, `"tomorrow 09:00"`, `"friday 8am"`, `2026-08-06 15:00` |
| `--auto` | reads the usage-limit reset out of the session itself |

Bare times that have already passed roll to tomorrow. A bare number (`90`) or a
bare hour (`15`) is rejected rather than guessed — write `90m` or `15:00`.

You can also skip the flag and let it classify:

```sh
claude-followup schedule claude4 90m continue
claude-followup schedule claude4 3pm run the test suite
```

The message is everything after the timing, so quoting is usually optional.
Use `-m` when the message starts with a dash.

### `--auto`: scheduling off the usage limit

`--auto` looks for Claude Code's usage-limit notice in two places — the live
pane (`dump-screen` / `capture-pane`) and the session's JSONL transcript under
`~/.claude/projects/` — and takes the earliest reset still in the future.

It understands both shapes Claude Code emits:

```
You've hit your session limit · resets 5pm (UTC)
You've hit your weekly limit  · resets Aug 8, 9am (UTC)
```

The dated weekly form matters: without the date it would schedule days early.

Check what it found before trusting it:

```console
$ claude-followup detect claude4
session zellij:claude4
  reset  2026-08-06 03:00:00 UTC  (in 4h00m)
  stamp  3am (UTC)
  source transcript
  would fire 2026-08-06 03:00:30 UTC (+30s)
```

If no future reset is found, `--auto` fails loudly rather than picking a time.

### Managing what's queued

```console
$ claude-followup list
WHEN                        IN  SESSION          MESSAGE
2026-08-05 19:28:17 UTC    1h00m  claude4          continue
2026-08-05 23:59:30 UTC    5h31m  claude2          write up the findings

$ claude-followup cancel claude4
cancelled -> claude4: continue
```

`cancel` takes a session name (cancels every follow-up for it), a unit name, or
`--all`. Queued jobs are transient systemd units, so they also disappear on
reboot and clean themselves up after firing.

Every command takes `--json` for scripting, and `--dry-run` where it changes
something.

## How it works

1. Resolve the session name to zellij or tmux (`--backend` forces one).
2. Work out the target time, add a small buffer.
3. `systemd-run --user --on-active=<delay>s` a transient timer that re-invokes
   `claude-followup send`.
4. On fire, type the message and a carriage return — `write-chars` + `write 13`
   on zellij, `send-keys -l` + `Enter` on tmux. The separate CR matters:
   Claude's TUI ignores a literal `\n` in pasted text.

Metadata lives in the unit's description and fire time is encoded in the unit
name, so `list` needs no state file of its own.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_FOLLOWUP_BUFFER` | `30` | seconds added after the target time |
| `CLAUDE_HOME` | `~/.claude` | Claude Code state directory |

`--buffer` overrides per-invocation. The buffer exists because a reset stamp is
rounded to the minute and firing a second early just burns the prompt.

## Limitations

- **Linux only.** Delivery is systemd `--user`. macOS would need launchd.
- **`--auto` is coupled to Claude Code's wording.** It is pinned by tests
  against real transcript strings; if Anthropic changes the message, those
  tests fail and tell you what to fix. Use `detect` to sanity-check.
- Typing into a session is exactly as safe as typing into it yourself — if the
  pane isn't sitting at a Claude prompt, the text goes wherever the cursor is.

## Development

```sh
python3 -m unittest -v     # no test dependencies
```

Tests cover the parsers — durations, clock times, and the usage-limit
scraping — which is where the bugs actually are. The zellij/tmux/systemd calls
are thin shell-outs and are left to integration testing by hand.

## License

MIT
