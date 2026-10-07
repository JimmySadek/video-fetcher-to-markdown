---
status: fixes on branch fix/trust-hub-audit (local; not pushed)
date: 2026-10-07
---

# Why skills.sh shows "Gen Agent Trust Hub: FAIL"

## Short answer

**Mostly a false positive, with two real triggers we can remove cheaply.** The audit's own text calls the skill's
safety posture good. What failed it was (1) a link to `codeguilds.dev`, a young domain that URL-reputation services
mark as risky, inside an old planning page in `specs/`, and (2) "the metadata file", which the scanner flagged
without saying why. There is no malicious or hidden behaviour in the skill.

⚠️ **The FAIL is about an old version.** The audit ran on **2026-09-07** (v1.2, YouTube only, before
`fetch_media.py`, the browser snippet and the rename). The listing page still shows the pre-v2.0 SKILL.md. v2.0 has
not been audited yet, and it adds new things a scanner will look at (downloads, a browser snippet, an opt-in cookie
flag).

## What the audit is

skills.sh shows audits from Gen Digital's **Agent Trust Hub** (the Norton/Avast company), Socket and Snyk. The Gen
audit is an LLM-plus-scanner review that tags risk categories and gives a verdict: `pass`, `warn` or `fail`, with a
risk level. The detail page (`/security/agent-trust-hub`) currently returns a server error (HTTP 500) for every
skill tested, including `vercel-labs/skills/find-skills`, so the evidence below comes from the public audit API:

```
curl -s "https://skills.sh/api/v1/skills/audit/jimmysadek/youtube-fetcher-to-markdown/youtube-fetcher"
```

Result for this skill (2026-10-07):

| Provider | Verdict | Risk | Audited |
|---|---|---|---|
| Gen Agent Trust Hub | ❌ fail | CRITICAL | 2026-09-07 |
| Socket | ✅ pass | (no alerts) | 2026-09-07 |
| Snyk | ✅ pass | LOW | 2026-09-07 |
| Runlayer | ✅ pass | LOW, "1/5 files flagged" | 2026-03-07 |

Gen's summary, quoted in part: "demonstrates good safety posture ... However, automated scanners have flagged the
metadata file and a documentation link to codeguilds.dev." Categories: `INDIRECT_PROMPT_INJECTION`,
`COMMAND_EXECUTION`, `EXTERNAL_DOWNLOADS`, `DYNAMIC_EXECUTION`, `METADATA_POISONING`.

## Evidence for each trigger

### 1. The codeguilds.dev link (confirmed)

- The only `codeguilds.dev` URL in the repo was in `specs/youtube-fetcher-v1-1-release-and-distribution.html`
  (a "Back references" link to the CodeGuilds listing).
- Public reputation checkers (for example Gridinsoft) list `codeguilds.dev` as a very young domain with heuristic
  fraud signals and no malware detections. A reputation hit on a linked domain is a typical reason for a CRITICAL
  rating even when the skill itself is clean.
- This also shows the scanner reads the whole repo folder, including `specs/`, not only `SKILL.md` and `scripts/`.

### 2. "The metadata file" (cause not stated; best inference)

- In v1.2, `agents/openai.yaml` held `display_name: "YouTube Fetcher"` and a `default_prompt`.
- The file format itself is not the problem: OpenAI's own skills ship the same file and pass
  (`openai/skills/playwright`, `figma`, `gh-fix-ci`, `screenshot`, all SAFE).
- Gen uses `METADATA_POISONING` for metadata that impersonates a trusted brand (one public example: a community skill
  whose frontmatter named Cloudflare as owner). A display name of "YouTube Fetcher" can read as claiming to be
  YouTube's. **This is inference, not confirmed.** v2.0 already changed the display name to
  "Video Fetcher to Markdown".

### 3. DYNAMIC_EXECUTION (likely code pattern)

- v1.2 `fetch_transcript.py` imported modules by a variable name (`importlib.import_module(dep["module"])`) in the
  dependency check.
- v2.0 `fetch_media.py` goes further: it loads `fetch_transcript.py` by file path and executes it
  (`spec_from_file_location` + `exec_module`). Scanners treat that pattern as dynamic code loading.

### The other categories are normal for this kind of skill

`COMMAND_EXECUTION`, `EXTERNAL_DOWNLOADS` and `INDIRECT_PROMPT_INJECTION` also appear on skills that pass with SAFE
(for example `vercel-labs/skills/find-skills` and `openai/skills/playwright`). The skill already tells the agent to
treat captions, descriptions and on-screen text as untrusted data.

## Fixes on branch `fix/trust-hub-audit`

| Fix | File | Behaviour change |
|---|---|---|
| ✅ Removed the `codeguilds.dev` link, kept the words "CodeGuilds listing" | `specs/...v1-1-release-and-distribution.html` | None |
| ✅ Load the shared helpers with a normal `import fetch_transcript as core` instead of executing the file by path | `scripts/fetch_media.py` | None |
| ✅ Dependency check looks packages up with `importlib.util.find_spec` instead of importing them | `scripts/fetch_transcript.py` | None (it no longer runs package code just to check it exists) |
| ✅ "Operational boundaries" now describes v2.0 accurately: which tools run, never through a shell, fixed yt-dlp flags; a new **Credentials** line says cookies are opt-in only and the browser snippet only reads the page | `SKILL.md` | Docs only |

Checks: `python3 -m unittest discover -s tests` (77 tests pass, same as before), `python -m py_compile` on both
scripts (the CI steps), `--check-deps` on both scripts, and `fetch_media.py --help` from another folder.

## Proposed, not done (needs Jimmy)

1. **Get a re-audit.** Nothing changes on skills.sh until Gen scans again. When or how it re-scans is not documented
   in what I found (looked at the listing, `/audits`, `/docs/api`). The listing still uses the old repo path; whether
   re-scans follow GitHub's redirect to `video-fetcher-to-markdown` **needs checking** after the next push.
2. **Stop shipping internal planning docs.** `specs/` is part of every install and gets scanned. Moving it out of the
   skill folder (or to a docs branch) shrinks what scanners and users see. Bigger change, so it is your call.
3. **Add a one-line "not affiliated with YouTube or Google" note** to README and SKILL.md, in case the metadata flag
   is a brand-impersonation reading. Cheap, but it is public wording and the cause is unconfirmed.
4. **Tests still use `spec_from_file_location`** to load the scripts. They ship with the skill too. Converting them
   is low value unless a re-audit still reports `DYNAMIC_EXECUTION`.

Not pushed, merged or released. Pushing the branch and any release need your yes.
