# Subscription usage

`vp usage` reads provider-reported subscription allowances. It does not derive
remaining quota from token counts or API prices. Session, weekly, and model-group
windows stay separate; several containers sharing a profile do not have their
allowances added together.

## Start collecting

Sign in normally through the official clients inside VibePod. The collector never
asks for a password, token, cookie, or exported browser session.

```bash
vp usage setup-claude
vp usage refresh
vp usage show --json
vp usage dashboard
```

Open `http://127.0.0.1:8002`. For ongoing collection, run `vp usage watch` in another
terminal. It stays in the foreground until Ctrl-C and waits 300 seconds between
checks. `--interval` accepts values of at least 60 seconds. Opening or refreshing
the browser page only reads cached snapshots; it never starts a provider request.

| Provider | Collection | Requirements |
| --- | --- | --- |
| Codex | Official app-server `account/rateLimits/read`, after initialization | A running, signed-in `vp run codex` container with Node and `timeout` (standard image) |
| Claude | Official status-line JSON `rate_limits` | `vp usage setup-claude`, then a normal response in interactive Claude Code |
| Antigravity (`agy`) | `agy -p /usage --output-format json` | A running, signed-in `vp run agy` container with CLI 1.1.11+ and `timeout` |

Only subscription clients are supported. Containers routed through `--provider`
are excluded. Unsupported versions, missing logins, and unknown schemas are
reported as unavailable. No agent containers, model turns, or login flows are
started by the collector. Global trusted CLI configuration may still initialize
client components; these are the installed official clients, not isolated parsers.

### Traffic capture

Codex and Antigravity checks make authenticated requests. VibePod's traffic proxy
can record authentication traffic, so probes refuse containers carrying a
`vibepod.proxy-policy` label or nonempty `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`
variables (either case). This also covers older containers without policy labels
and explicit proxies whose capture behavior cannot be verified. Probes never
silently bypass the configured proxy.
Use a container started with this configuration for these checks:

```yaml
proxy:
  enabled: false
```

An already-running container keeps its original proxy settings; start a new one
after changing the configuration and removing any explicit proxy variables.
Custom images, transparent proxies, CLI hooks, and the underlying machine still
need to be trusted. Passive Claude status-line capture makes no additional requests and
does not alter existing traffic capture.

### Profiles and multiple containers

```bash
vp usage refresh --provider codex --profile work
vp usage refresh --provider agy --profile work --container vibepod-agy-example
vp usage setup-claude --profile work
vp usage watch --profile work
```

Profile resolution follows the normal CLI flag, `VP_PROFILE`, and configuration
precedence. `antigravity` is an alias for the `agy` provider argument. When several
matching containers are running, select one with `--container` rather than guessing
which session to inspect. A profile is a local credential scope, not a verified
provider account identity. Snapshots are labeled by profile, never by email.

## Claude status-line capture

Setup wraps the selected profile's existing `statusLine.command` and preserves the
other status-line settings. The wrapper receives Claude Code's normal JSON on
stdin, writes only numerical usage fields, then forwards the original input to
the original status-line command. Without an existing command it displays remaining
usage. It does not read transcripts or authentication files.

Claude only supplies these fields after an API response for supported subscription
plans. Headless/ACP runs may not invoke a status line. Missing fields remain
unavailable. Idle redraws with unchanged quota/response counters do not advance
the observation time. Calls made elsewhere can change your allowance without
updating this snapshot; use Claude's official Usage page for an independent check.

Restore the original status line with:

```bash
vp usage setup-claude --disable
```

Setup leaves the user's configuration in place if they replaced the wrapper with
a different command. Sanitized snapshots are retained after disabling capture.

## Data and access boundaries

The dedicated store is `<VibePod config root>/usage/subscriptions.db`. New files
are owner-readable/writable and the directory is owner-only on POSIX. It contains
provider IDs, profile labels, numerical quota windows, observation/check times,
and fixed diagnostic codes. Responses, credentials, HTTP headers, account emails,
provider error text, and conversation content are not stored. Unexpected provider
group names receive neutral numbered labels. Changing accounts within a profile
requires a new successful observation; until then, its snapshot may describe the
previous account.

The dashboard binds to `127.0.0.1` only, rejects foreign Host/Origin headers, serves
bundled assets without third-party scripts, and offers a read-only `/api/usage`
endpoint. It has no provider refresh endpoint, Docker socket mount, or arbitrary
file endpoint. It runs as your user, so this is a narrow data interface, not an
OS sandbox or protection against a compromised machine. Do not publish this HTTP
server directly. A future hosted dashboard should receive only these sanitized
snapshots over authenticated HTTPS.

The existing `vp logs` dashboard has broader access to VibePod's configuration
directory and captured traffic; `vp usage dashboard` does not launch or modify it.

## Freshness and failure handling

- `observed_at` is when the provider data was captured; `checked_at` is when the
  collector last checked it. They are not interchangeable.
- Snapshots older than five minutes and failed refreshes are marked stale.
- A window whose reset time has passed remains stale until observed again; no
  automatic refill or new allowance is invented.
- Failed checks preserve the last successful values, with an unavailable status.
- A missing percentage is unknown, not zero. Disabled quotas are unavailable.
- `refresh` exits nonzero when any selected provider fails, while `--json` still
  emits a complete, sanitized snapshot. `watch` keeps retrying at its configured
  interval; no tight retry loop is used.

## Provider references

- [Codex app server](https://learn.chatgpt.com/docs/app-server)
- [Claude Code status line](https://code.claude.com/docs/en/statusline)
- [Antigravity CLI changelog](https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md), version 1.1.11

Tests use synthetic provider replies and fake executables. They exercise quota
parsing, credential-field exclusion, profile isolation, stale/error states,
the Codex handshake, the Claude wrapper, and the dashboard access boundary. Real
subscription accounts are not required for the test suite.
