# Deploying SirisAI + SirisOS with Docker Compose

This folder runs both apps on one server, already wired together. SirisOS
reaches SirisAI at `http://sirisai:8000`, and SirisAI reaches SirisOS at
`http://sirisos:6464`, both on the stack's own network. The keys they use
for each other are generated once and kept in `.env`.

## First time

On the server, with Docker and the Compose plugin installed:

```bash
git clone https://github.com/bradmairs/SirisAI.git
cd SirisAI/deploy
./setup.sh        # secrets, settings files, data folders, SirisOS checkout
```

`setup.sh` is safe to re-run and never overwrites a file you've edited. It:

- creates `.env` with three random secrets;
- clones SirisOS next to SirisAI (`../../SirisOS`, or wherever `SIRISOS_SRC`
  in `.env` points);
- checks the SirisOS checkout is on `main`. The SirisAI integration
  (SirisOS ADR 110) used to be applied here as a patch on a local branch
  called `sirisai-integration`. It's in SirisOS main now, so a checkout
  still on that branch is told to switch back;
- creates `sirisai.env` and `sirisos.env` from each app's own `.env.example`,
  so every setting is listed there with its explanation.

Then edit three things and start:

1. **`sirisai.env`**: set a model, for example
   `SIRISAI_LLM_PROVIDER=ollama` and
   `SIRISAI_OLLAMA_BASE_URL=http://host.docker.internal:11434` (Ollama
   running on this server) or its LAN address. Then add Home Assistant,
   weather and ntfy as you want them.
2. **`sirisos.env`**: set `SIRISOS_ADMIN_PASSWORD`, then the apps SirisOS
   connects to. The example addresses (192.168.0.100) show as "down" until
   they're right.
3. **`.env`**: set `SIRIS_HOST` to the server's address as you type it in a
   browser, and the ports if 8000 or 8094 are taken.

```bash
make up       # build and start both
make check    # both healthy, each reaching the other, a model configured
```

- SirisAI: `http://<server>:8000`, using the `SIRISAI_API_KEY` from `.env`
  in its web UI.
- SirisOS: `http://<server>:8094`, signing in with `SIRISOS_ADMIN_USERNAME` /
  `SIRISOS_ADMIN_PASSWORD`.

## What to edit, where

| File | What's in it | Apply with |
| --- | --- | --- |
| `.env` | Server address, ports, timezone, the shared secrets, where SirisOS's source is | `make up` |
| `sirisai.env` | Every SirisAI setting (model, Home Assistant, cameras, email, car...) | `make restart` |
| `sirisos.env` | Every SirisOS setting (login, the apps it connects to, the brief) | `make restart` |
| `docker-compose.yml` | The wiring between the apps, volumes, ports | `make up` |

Lines starting `# set by docker-compose.yml:` in the two app files are set by
the compose file, so the two apps can't disagree about addresses or keys.
Change those in `.env`, or in `docker-compose.yml`.

## Everyday commands

```
make up        build and start          make logs       follow logs (make logs s=sirisos)
make restart   apply edited *.env       make ps         what's running
make rebuild   after pulling code       make check      health and wiring
make update    git pull both + rebuild  make backup     tar ./data into ./backups
make https     add HTTPS                make down       stop everything
make dev       live-edit mode (below)   make shell-ai / shell-os   a shell inside
```

## Editing the code on the server

`make dev` runs both apps straight from your checkouts. Saving a Python file
in `SirisAI/app/` or `SirisOS/apps/backend/app/` reloads that app within a
second or two, with no rebuild. SirisOS's web app is built into its image,
so for frontend work either `make rebuild` after changes, or run Vite on
your own machine against the server:

```bash
cd SirisOS/apps/web && SIRISOS_API_URL=http://<server>:8094 npm run dev
```

`make up` goes back to the normal images. Do that before relying on the
server day to day, since live-edit mode watches files constantly.

## HTTPS (for the microphone)

Browsers only allow the microphone on HTTPS. `make https` adds Caddy with
its own certificate authority:

- SirisAI on `https://<SIRIS_HOST>:8443`
- SirisOS on `https://<SIRIS_HOST>:8444`

To get rid of the certificate warning, trust Caddy's root certificate once
per device. Download it from `https://<SIRIS_HOST>:8443/siris-root-ca.crt`
(click through the warning one last time), then:

- **iPhone/iPad:** open it in Safari → Allow → Settings → Profile Downloaded
  → Install. Then go to Settings → General → About → Certificate Trust
  Settings and turn on "Caddy Local Authority".
- **Mac:** open it, then in Keychain Access set "Caddy Local Authority" to
  Always Trust.

Caddy's keys live in `data/caddy`. Keep that folder, or every device will
need to trust a new root.

## Updates that apply themselves

```bash
make auto-update   # once: installs the updater's cron entry
```

When a new SirisAI release is out, SirisAI says so in the attention feed
(SirisOS's inbox) with the release notes and an **Update now** button. Set
`SIRISAI_UPDATE_MODE=auto` in `sirisai.env` to skip the button and update at
`SIRISAI_UPDATE_CHECK_HOUR_LOCAL` (9am) instead.

`updater.sh` (run every minute by cron, doing nothing unless asked):

1. Refuses if either checkout has local edits, so your changes on the
   server are never overwritten. Commit or stash them first.
2. Fast-forwards SirisAI to the release tag, pulls SirisOS, rebuilds and
   restarts.
3. Checks both apps answer, SirisAI reports the new version, and its tool
   selection eval is no worse than before.
4. If anything fails, puts both checkouts back where they were, rebuilds,
   and tells you why ("Update to 1.3 rolled back: ...", pushed to your
   phone). Its log is `data/sirisai/update/update.log`.

`./updater.sh uninstall` removes the cron entry. `make update` still
updates by hand, without the checks.

## SSH to your other servers

SirisAI can check on and run commands on other machines on your network
("how's the NAS?", "restart nginx on the web box"). Give it a key once, then
name the servers in `sirisai.env`:

```bash
ssh-keygen -t ed25519 -N "" -C sirisai -f data/sirisai/ssh/id_ed25519
ssh-copy-id -i data/sirisai/ssh/id_ed25519.pub brad@192.168.0.127   # once per server
echo 'SIRISAI_SSH_HOSTS=nas=brad@192.168.0.127,pi=root@192.168.0.83' >> sirisai.env
make restart
```

The key lives in `data/sirisai/ssh`, mounted as the container's `~/.ssh`, so
it survives rebuilds. Commands that only look (uptime, disk, memory, service
status, logs) run straight away; anything that changes something asks you
first, showing the server and the exact command. Use a non-root account on
each server unless you want SirisAI able to do root things with your OK.

### Reboot and shut down

"Reboot the NAS", "shut down all the servers in 10 minutes" and "cancel the
shutdown" always ask first (tap Allow or Approve; a spoken "yes" isn't
enough). With a non-root account, allow just the power commands without a
password, on each server:

```bash
echo "brad ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/sbin/shutdown" | sudo tee /etc/sudoers.d/sirisai
sudo chmod 440 /etc/sudoers.d/sirisai && sudo visudo -cf /etc/sudoers.d/sirisai
```

(Check the paths with `which systemctl shutdown`.) If SirisAI itself runs on
one of the servers, set `SIRISAI_SSH_SELF_SERVER` to its name, so "all" does
that one last. Note that shutting down the server SirisAI runs on takes
SirisAI down with it, until someone turns it back on.

### Wake, watch, updates and disks

- **Wake-on-LAN** ("wake the NAS"): turn it on in the server's BIOS and
  network settings. SirisAI learns each server's MAC while it's up. A
  container can't broadcast onto your LAN, so either set
  `SIRISAI_SSH_WAKE_RELAY` to a server that stays on (it sends the packet for
  SirisAI), or enable Home Assistant's `wake_on_lan:` integration.
- **The server watch** checks every 15 minutes and puts disks over 90%,
  failed services and servers that stop answering in the attention feed,
  with Restart and Wake buttons. Each morning it also lists waiting OS
  updates (Install updates, and Reboot when one is needed) and checks the
  disks (SMART, ZFS, RAID).
- **Permissions:** ask "what permissions are you missing on the NAS?". SirisAI
  checks with sudo and gives the exact line to add for what's missing. Rules
  must name exact commands: Ubuntu 25.10's `sudo-rs` doesn't accept wildcards
  in arguments. Restarting a service takes one rule per service, e.g.
  `brad ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx.service`.
  Allowing apt lets that account install packages as root, so leave it out if
  that's too much. SMART needs `smartmontools` installed.

## Data and backups

Everything that matters is in `./data`:

- `data/sirisai`: SirisAI's database and SSH identity
- `data/sirisos`: SirisOS's state, standards library and logs
- `data/caddy`: the HTTPS certificate authority

SirisAI backs up its own database every night at 2am into
`data/sirisai/backups` (a consistent copy while it runs, the newest 14
kept), and on the 1st of each month restores the newest one into a scratch
file to prove it opens. Set `SIRISAI_BACKUP_COPY_TO` to a folder mounted
from another disk or a NAS (add the mount under the sirisai service's
`volumes`) to keep a copy off this disk. `make backup` still writes a dated
archive of all of `./data` to `./backups`; for a guaranteed-consistent copy
of SirisOS's files with it, run `make down` first.

Container logs are capped at 30 MB per app. `.env`, `sirisai.env`, `sirisos.env`, `data/` and `backups/` are
git-ignored.

## Notes

- **Docker socket:** SirisAI mounts `/var/run/docker.sock` so self-healing
  and the `docker_*` tools can see and restart containers. Remove that line
  from `docker-compose.yml` if you'd rather it couldn't.
- **Second Brain:** to use the Second Brain, clone the vault into
  `data/sirisai/second-brain` and set
  `SIRISAI_BRAIN_PATH=/app/data/second-brain` in `sirisai.env`.
- **Existing setup:** if you already run SirisAI or SirisOS with their own
  compose files, stop those first (`docker compose down` in each). This
  stack uses the same ports. To keep your history, copy SirisAI's `data/`
  into `data/sirisai/`, and SirisOS's `data/app/` and `data/standards/` into
  `data/sirisos/app/` and `data/sirisos/standards/`.
