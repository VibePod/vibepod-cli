# Snap distribution

The Snap recipe and release workflow are in this repository. Store publication
is pending registration of `vibepod`, approval for classic confinement, and
configuration of publisher credentials. The Store commands below apply once
that setup is complete; use pip, Homebrew or conda-forge until then.

## Install on Linux

Install and enable [snapd for your distribution](https://snapcraft.io/docs/installing-snapd).
Ubuntu commonly includes snapd. Ensure `/snap/bin` is on your `PATH` (log out
and back in after installing snapd if needed). The initial package targets
Linux amd64 and needs a system supporting the `core24` base; Ubuntu Core is
not supported because this package requires classic confinement.

Install Docker or Podman separately and make its API socket accessible to your
user. See [runtime prerequisites](quickstart.md#prerequisites). The snap bundles
Python, the CLI's Python dependencies and CA certificates, but no container
engine or agent images. You do not need a host Python installation.

```bash
sudo snap install vibepod --classic
vibepod version
vibepod run claude
```

The canonical command is `vibepod`. The short command is `vibepod.vp`; optionally
create the usual alias (provided another installation does not own it):

```bash
sudo snap alias vibepod.vp vp
vp version
```

Do not run the CLI with sudo to work around socket permissions. Configure
Docker access for your user or start the rootless Podman socket:

```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock
vibepod run claude
```

## Updates and removal

Snapd automatically refreshes packages. To update immediately or opt into
prereleases:

```bash
sudo snap refresh vibepod
sudo snap refresh vibepod --channel=latest/beta
# Return to stable releases:
sudo snap refresh vibepod --channel=latest/stable
snap info vibepod
```

Remove the package with:

```bash
sudo snap remove vibepod
```

Vibepod uses the same host configuration, credentials and project files as
other installation methods. Removing the snap does not remove those files,
agent images, containers or runtime volumes. Stop sessions before removal.

## Confinement and permissions

Strict confinement was considered first. Vibepod accepts arbitrary workspace
paths, mounts agent credentials and skills from hidden home directories,
connects to Docker or rootless Podman API sockets, and invokes host tools such
as `podman`, `xauth` and optionally `herdr`. A `home` plug alone does not cover
these paths. The [docker interface](https://snapcraft.io/docs/reference/interfaces/docker-interface/)
provides access to the Docker snap's daemon, not the full supported host-runtime
contract. A restricted Docker-only variant would need a separate support policy
and validation.

The initial recipe therefore declares `confinement: classic`. It grants the
CLI the same host access as a pip installation, subject to Unix permissions;
it does not add a Snap sandbox around the CLI. Agent isolation remains the
responsibility of Docker or Podman. No interface connections are required or
declared for this classic snap. The
[Store must approve classic confinement](https://snapcraft.io/docs/reference/administration/reviewing-classic-confinement-snaps/)
before publication, and users explicitly accept it with `--classic`.

Container API access allows image pulls, container execution, workspace and
credential bind mounts, networks and published ports. Local metrics and Herdr
use the existing host paths and permissions. HTTP tracking, the proxy and the
dashboard run through the container runtime and retain their existing network
and port settings. No `docker-support`, `system-observe`, `network-bind` or
additional privileged interface is requested. Classic confinement does not
bypass runtime permissions or firewall rules.

## Build, validate and release

On a snapd-enabled Linux amd64 build host, install Snapcraft and its supported
build provider, then build from the repository root:

```bash
sudo snap install snapcraft --classic
snapcraft
sudo snap install --dangerous --classic ./vibepod_*.snap
export VP_SNAP_EXPECTED_VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
export VP_CONFIG_DIR="$(mktemp -d)"
snap run vibepod version
snap run vibepod.vp version
snap run --shell vibepod <<'SH'
exec "$SNAP/bin/python3" "$PWD/scripts/smoke_snap.py"
SH
```

Use Python 3.11+ for the version-extraction command above. `--dangerous` is only
for local, unsigned artifacts. Keep Snapcraft's classic and library lint checks
enabled; fix their findings before publishing. The smoke test runs using the
packaged interpreter and dependencies, verifies that imports come from the snap
and that both installed commands report the release version, pulls an image,
runs a container, checks host workspace reads and writes, a hidden configuration
directory bind mount and environment injection, and exercises the installed
CLI's container listing and stop commands. It also fetches HTTP content through
a dynamically assigned localhost port, validating the port publishing used by
the proxy and dashboard. It removes its test container even
if validation fails. A running Docker
or Podman API is required. Repeat with `DOCKER_HOST` set to a rootless Podman
socket before claiming support on a new runtime or base. Before the first Store
release, also exercise an authenticated agent session, HTTP tracking and the
dashboard on the installed snap.

`.github/workflows/snap.yml` builds and smoke-tests pull requests that affect
packaging or CLI code against Docker and rootless Podman; manual dispatch
validates without publishing. Both runtimes must pass before upload. Published
GitHub releases build from their release tag and publish only after the unit
suite and installed-package smoke test pass. The version is read directly from
`pyproject.toml` via `adopt-info`; a release tag must match it (with an optional
`v` prefix). Normal releases go to `latest/stable`, prereleases to `latest/beta`.
The tested amd64 artifact is retained on the workflow run. No main-branch or
pull-request build uploads to the Store.
Release runs fail early with a setup error if publisher credentials are missing.

One-time publisher setup:

1. Log in with the project's Snap Store publisher account and run
   `snapcraft register vibepod`. Confirm ownership and the finalized package
   name; if unavailable, update the recipe and installation docs together.
2. Request classic confinement approval with the rationale above. Do not
   advertise Store availability until approval and the first upload succeed.
3. Export a package-scoped credential with `snapcraft export-login`, restricted
   to `vibepod`, `package_upload`/`package_release`, and the intended channels.
   Store its contents as the GitHub Actions secret
   `SNAPCRAFT_STORE_CREDENTIALS`; never commit it. Rotate before expiration.
4. Publish a matching GitHub release and verify `snapcraft status vibepod`,
   `snap info vibepod`, and a clean `snap install vibepod --classic` on another
   machine. Remove the pending-publication notice in this page and the install
   docs only after this succeeds.

For a failed release, inspect the saved artifact and workflow logs. A maintainer
can upload the validated artifact with `snapcraft upload <artifact.snap>
--release=latest/stable` (or `latest/beta`) after fixing Store setup. Roll back a
bad channel revision with `snapcraft release vibepod <previous-revision>
latest/stable`; do not rebuild a different version under an existing CLI tag.
