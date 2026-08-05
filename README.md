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

It's one file with no dependencies, so the simplest install is a download.
This sidesteps PEP 668 (`externally-managed-environment`) entirely — nothing
is installed into a Python environment:

```sh
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/claude-followup \
  https://raw.githubusercontent.com/poruchik-bi/claude-followup/main/claude_followup.py
chmod +x ~/.local/bin/claude-followup
ln -sf ~/.local/bin/claude-followup ~/.local/bin/cf   # optional short alias
```

Make sure `~/.local/bin` is on your `$PATH`.

<details>
<summary>Or install as a managed package</summary>

```sh
uv tool install git+https://github.com/poruchik-bi/claude-followup
# or
pipx install git+https://github.com/poruchik-bi/claude-followup
```

On Debian/Ubuntu, `pip install --user` fails with
`error: externally-managed-environment` — that is PEP 668 protecting the
system Python, not a problem with this package. Use `pipx`
(`sudo apt install pipx`), `uv`, or an explicit venv:

```sh
python3 -m venv ~/.local/share/claude-followup
~/.local/share/claude-followup/bin/pip install git+https://github.com/poruchik-bi/claude-followup
ln -sf ~/.local/share/claude-followup/bin/claude-followup ~/.local/bin/
```

Do not reach for `pip --break-system-packages`; there is nothing to install
into the system Python in the first place.

</details>

Either way you get `claude-followup` and the short alias `cf` — the same tool,
and jobs queued with one are listed and cancelled by the other. Help text and
errors report whichever name you used.

```sh
cf schedule claude4 --auto -m "continue"
```

`cf` is also Cloud Foundry's CLI name. If that clashes, use the download
install and skip the `ln -s`, or symlink it as whatever you prefer.

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

If the limit has **already lifted**, `--auto` sends the message straight away
rather than scheduling — "deliver when the limit allows" means now:

```console
$ claude-followup schedule claude4 --auto -m continue
limit already reset at 2026-08-05 19:00:00 UTC (zellij pane) -- nothing to wait for
sent -> zellij:claude4: continue
```

Use `--dry-run` if you want to see which way it will go without typing
anything. If no reset is found at all, `--auto` fails loudly rather than
guessing a time — use `--in` or `--at` for those.

#### When `--auto` finds nothing

`detect --explain` shows every place it looked and what it saw:

```console
$ claude-followup detect claude4 --explain
zellij pane: captured 839 chars
  lines mentioning a limit: 0
  reset stamps matched: none
claude session id: 04d064dd-3389-40d6-b8cc-ec55d8efccb6
  transcripts searched: 1
    04d064dd-....jsonl: 0 rate-limit entries
candidates: 0
claude-followup: no usage-limit reset found for 'claude4'
```

That output is the diagnosis. The common causes:

- **The notice scrolled out of the pane.** Claude Code's TUI repaints its own
  viewport, so old output never reaches the multiplexer's scrollback — once
  it's gone from the TUI, it's gone. `--auto` is most reliable run soon after
  you hit the limit.
- **The transcript has no `rate_limit` entry.** Claude Code does not always
  record one, so the pane is often the only source. `0 rate-limit entries`
  with `0` limit lines on the pane means there is genuinely nothing to read.
- **The pane wasn't the Claude one.** Both backends dump the session's active
  pane; if that's a shell, there's nothing to find.

When there's no evidence, reach for `--at` with the time you saw:

```sh
claude-followup schedule claude4 --at 12am -m continue
```

An undated stamp like `resets 7pm` is read as the occurrence *nearest* the
moment it was written, not the next one after it. Seen at 19:04, that is 19:00
today — four minutes ago — not 19:00 tomorrow.

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

### Checking what you're running

Because this gets copied between machines as a loose file, the version number
alone can't tell you whether two installs are the same code. `-v` reports a
hash of the file itself, so you can compare:

```console
$ claude-followup -v
claude-followup 0.2.0 (build b0bccfe)
```

Same build id means byte-identical code. Different id, same version, means one
machine is stale — re-copy the file.

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
