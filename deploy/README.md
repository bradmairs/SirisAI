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
- if that checkout doesn't have the SirisAI integration yet (SirisOS ADR 110),
  applies `patches/sirisos-adr110.patch` on a branch called
  `sirisai-integration`. Once that change is merged into SirisOS, nothing is
  applied;
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

## Data and backups

Everything that matters is in `./data`:

- `data/sirisai`: SirisAI's database and SSH identity
- `data/sirisos`: SirisOS's state, standards library and logs
- `data/caddy`: the HTTPS certificate authority

`make backup` writes a dated archive of it to `./backups`. For a
guaranteed-consistent copy of SirisAI's SQLite database, run `make down`
first. `.env`, `sirisai.env`, `sirisos.env`, `data/` and `backups/` are
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
