# Working on SirisAI

## Versions: bump with every change

SirisAI was released as **1.0**. Every change merged to `main` bumps the
version in the same PR:

- **+0.01**: small changes, such as a fix, a tweak, a setting, a small
  tool, docs, or tests. `python scripts/bump_version.py small "<one line>"`
- **+0.1**: significant changes, such as a new feature, a new integration,
  or behaviour people will notice. `python scripts/bump_version.py significant "<one line>"`

The script updates `app/__init__.py` and adds a dated section at the top of
`CHANGELOG.md`; add bullets there if one line isn't enough. Versions are
decimals shown with at least one place: 1.0, 1.01, 1.1, 1.13. When a PR
holds several changes, bump once, by the size of the largest. Once a new
version reaches `main`, `.github/workflows/release.yml` tags it
`v<version>` and publishes a GitHub release from its changelog section.
The Docker image is tagged with the version too. Never tag by hand.

## Merging: automatic

Merge your own PRs to `main` yourself, without asking, once CI is green
on the current head and there's no conflict. Use a squash merge, titled
"<summary> (#<number>)". Bump the version and update the changelog before
merging. Don't merge when CI is red, a review asks for changes, or the
PR touches something the user asked to approve first.

## Conventions

- Each feature gets a numbered milestone in `README.md` ("NNN. **Done** -- ...")
  describing what it does and how it was verified.
- Run the full suite before pushing: `python -m pytest -q`. Better, turn on
  the pre-push hook once per clone (`git config core.hooksPath .githooks`):
  it runs `scripts/check.sh`, which clears bytecode caches, checks the hub
  contract schema and runs the suite on Python 3.12 (CI's version) when
  it's installed. The suite shares one
  SQLite database across tests, so a test that needs tables should take
  the `client` fixture. Run a single file after `tests/test_api.py`, or it
  will fail on missing tables.
- The hub contract (`/siris/hub/v1`) is shared with SirisOS. After changing
  `app/hub/contract.py`, run `python -m app.hub.contract` and
  `HUB_WRITE_EXAMPLES=1 pytest tests/test_hub_examples.py`. Removing or
  retyping a field is a v2.
- `deploy/` runs SirisAI and SirisOS together; keep it working when
  settings or ports change.

## Siris: record what you build (Brad's standing instruction)

Siris is Brad's second brain, the private repo `bradmairs/Siris-Second-Brain`, and his manual for rebuilding everything. When a session in this repo builds, changes, deploys or retires a feature, record it in Siris before finishing, without being asked:

- Follow `plugins/siris-brain/skills/learn/SKILL.md` in that repo. In a cloud session, attach it first (owner `bradmairs`, repo `Siris-Second-Brain`, push access). Always write through its engine (`tools/brain.py learn`), never by editing notes directly, then commit and push.
- Add to this app's note, **"SirisAI"**, so it gets a dated update instead of a duplicate.
- Capture what changed and why, the version, how to deploy or update it, the settings that matter (names only), and lessons learned. Never write secrets.
- End your reply with one line, e.g. `🧠 Siris learned: [[SirisAI]] (updated)`.
