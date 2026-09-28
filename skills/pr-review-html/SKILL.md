---
name: pr-review-html
description: Review the code changes on a git branch (or the current branch) against a base branch and deliver the review as a self-contained, GitHub pull-request-style HTML page with the findings as inline comments on the diff, a file tree, light/dark mode and horizontally scrolling code. Use this whenever the user asks to review a branch, a feature branch, "my changes", a diff or a PR locally and wants a visual/HTML/browsable result — e.g. "review this branch and make it look like a GitHub PR", "turn your review into an html page", "local PR review page", "render the review with inline comments". Also use it when the user already got a review in chat and asks to turn it into a page.
---

# PR review as an HTML page

Two parts: do a real code review, then render it with the bundled script. The script handles all
HTML/CSS/JS, so never hand-write the page — spend the effort on the review itself.

## 1. Scope the change

- **Branch**: what the user named, otherwise the current branch. It may exist only on the remote
  (`origin/<branch>`) — run `git fetch` if a ref is missing.
- **Base**: what the user named; otherwise the repo's default branch (`origin/HEAD`, or
  `main`/`master`/`develop`). Project instruction files (AGENTS.md, CLAUDE.md, GEMINI.md, …) often state the base
  branch — prefer that.
- Use the three-dot diff (`git diff <base>...<branch>`) so only the branch's own changes count,
  and `git log <base>..<branch>` for the commits.
- Respect the project's sensitive-file rules: don't open files it tells you not to read.
- In zsh, quote `"${ref}:path"` when running `git show ref:path` — `$ref:a` / `$ref:c` are
  history modifiers and silently mangle the path.

## 2. Review

The value of this skill is the review, so review like a senior engineer, not a linter.

- **Read beyond the diff.** Open the surrounding code: callers, the old implementation, related
  tests, the interfaces touched. Most real bugs live in how the changed lines interact with code
  the diff doesn't show.
- **Focus on principle and implementation**: correctness, concurrency and threading, error and
  cancellation paths, ordering/race assumptions, API and module boundaries, whether the change
  achieves what the branch/commits claim, performance claims, missing or misleading tests.
- **No syntax or style nitpicking** unless the user asks for it. The `nit` label exists for the
  rare case that genuinely matters.
- **Verify before claiming.** Check a suspicion against the code (or library source) before
  writing it down; if you can't confirm it, phrase it as a question.
- **Make each comment self-contained**: what is wrong, the concrete scenario where it breaks, and
  a suggested direction. A reader skimming one comment should understand it without the others.
- Call out what is done well when it's worth knowing (`praise`) — sparingly.

Labels: `blocking` (should be fixed before merge), `suggestion`, `question`, `design`
(structural/readability trade-off, not a bug), `praise`, `nit`.

## 3. Write the review JSON

Anchor each comment on a line that **appears in the diff** (an added, removed or context line) —
that's where it renders. Use new-file line numbers (`"side": "new"`, the default); for a removed
line use `"side": "old"` with the old line number. Get exact numbers with
`git show <branch>:<path> | grep -n '<snippet>'` rather than guessing.

```json
{
  "title": "TICKET-123: Short PR title",
  "reviewer": "codex",
  "comments": [
    {
      "file": "src/player/PlayerExtensions.kt",
      "line": 72,
      "label": "blocking",
      "body": "A stale callback can resume the wait early. ...\n\n**Fix:** check the media item when the callback fires."
    }
  ]
}
```

Bodies support a small markdown subset: paragraphs, `- ` lists, `**bold**`, `` `code` ``,
`[links](https://…)` and ``` fenced blocks (for suggested code). Full field reference:
`references/review-format.md`.

Set `reviewer` to your own agent name (e.g. `claude`, `codex`, `pi`) so readers know who wrote the
comments. Save the JSON next to the output HTML so the page can be re-rendered later.

## 4. Render

```bash
python3 <skill-dir>/scripts/render_review.py \
  --repo <repo> --base <base> --head <branch> \
  --review <review.json> --out <review.html> \
  [--exclude '*.lock' --exclude '**/generated/**']
```

The script reads the diff, commits and author from git, and warns on stderr about comments whose
file isn't in the diff (dropped) or whose line isn't in a hunk (placed at the end of that file).
Fix those anchors and re-run — a misplaced comment is confusing. `--diff file.diff` renders from a
saved diff instead of git.

**Where to write it**: follow project instructions if they name a place for generated HTML
(e.g. a `generated/` folder outside the source tree). Otherwise use
`/tmp/pr-reviews/<repo>/<branch-slug>.html`. Never put it inside tracked source directories.

The page offers: Files changed (default) and Commits tabs; a collapsible folder tree with comment
counts that smooth-scrolls to files; inline comment threads with Resolve toggles; Viewed
checkboxes and collapse-all; code that never wraps and scrolls horizontally inside its card; a
system-following light/dark theme; and a layout that works down to phone width.

## 5. Show it and summarise

- Open the page (`open <file>` on macOS, `xdg-open` on Linux), or use a browser-preview tool if
  one is available (serve the folder with `python3 -m http.server` if the tool needs http).
- In chat, give a short summary: the most important finding(s) first, then the count of comments
  by label, and the file path. Don't repeat every comment — they're on the page.

To update after new commits or edits to the review: adjust the JSON and re-run the same command.
