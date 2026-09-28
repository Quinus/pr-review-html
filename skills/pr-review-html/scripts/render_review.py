#!/usr/bin/env python3
"""Render a git branch diff plus review comments as a self-contained, GitHub-PR-style HTML page.

Usage:
  render_review.py --review review.json --out review.html [--repo .] [--base main] [--head HEAD]
  render_review.py --review review.json --out review.html --diff changes.diff

The diff, commit list and author are read from git (three-dot diff: base...head) unless --diff is
given. Comments come from the review JSON; see references/review-format.md in the skill.
"""
import argparse
import fnmatch
import html
import json
import re
import subprocess
import sys
from pathlib import Path

LABELS = {
    "blocking": ("Blocking", "lbl-red"),
    "suggestion": ("Suggestion", "lbl-blue"),
    "question": ("Question", "lbl-purple"),
    "design": ("Design", "lbl-orange"),
    "praise": ("Praise", "lbl-green"),
    "nit": ("Nit", "lbl-gray"),
}

# Populated in main(); render helpers read it to count comments per file.
COMMENTS = []
REVIEWER = "agent-review"


def git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout


def default_base(repo: str) -> str:
    try:
        ref = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
        return ref.removeprefix("refs/remotes/")
    except subprocess.CalledProcessError:
        for candidate in ("main", "master", "develop"):
            if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", candidate],
                              capture_output=True).returncode == 0:
                return candidate
    sys.exit("Could not determine a base branch; pass --base.")


def inline_md(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def md(text: str) -> str:
    """Small markdown subset: paragraphs, - lists, ### headings, `code`, **bold**, links, ``` fences."""
    out, in_list, fence = [], False, None
    for raw in text.strip().split("\n"):
        line = raw.rstrip()
        if fence is not None:
            if line.startswith("```"):
                out.append(f'<pre class="md-code">{html.escape(chr(10).join(fence))}</pre>')
                fence = None
            else:
                fence.append(raw)
            continue
        if line.startswith("```"):
            if in_list:
                out.append("</ul>")
                in_list = False
            fence = []
            continue
        if line.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline_md(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("#"):
            out.append(f"<h3>{inline_md(line.lstrip('#').strip())}</h3>")
        elif line:
            out.append(f"<p>{inline_md(line)}</p>")
    if fence is not None:
        out.append(f'<pre class="md-code">{html.escape(chr(10).join(fence))}</pre>')
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def parse_diff(diff: str):
    files, cur = [], None
    old_no = new_no = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            m = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            path = m.group(2) if m else line.split(" b/", 1)[-1]
            cur = {"path": path, "rows": [], "add": 0, "del": 0, "status": None, "binary": False}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur["status"] = "new"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["status"] = "renamed"
            cur["renamed_from"] = line[len("rename from "):]
        elif line.startswith("Binary files"):
            cur["binary"] = True
        elif line.startswith(("index ", "--- ", "+++ ", "old mode", "new mode", "similarity index",
                              "rename to ", "copy from ", "copy to ", "dissimilarity index")):
            continue
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            old_no, new_no = int(m.group(1)), int(m.group(2))
            cur["rows"].append(("hunk", None, None, line))
        elif line.startswith("+"):
            cur["rows"].append(("add", None, new_no, line[1:]))
            cur["add"] += 1
            new_no += 1
        elif line.startswith("-"):
            cur["rows"].append(("del", old_no, None, line[1:]))
            cur["del"] += 1
            old_no += 1
        elif line.startswith("\\"):
            continue
        else:
            cur["rows"].append(("ctx", old_no, new_no, line[1:] if line else ""))
            old_no += 1
            new_no += 1
    return files


KEYWORDS = (
    "abstract|actual|as|async|await|break|by|case|catch|class|companion|const|constructor|continue|"
    "data|def|default|defer|del|delegate|do|elif|else|enum|except|expect|export|extends|extension|"
    "false|final|finally|fn|for|from|fun|func|function|get|go|guard|if|impl|implements|import|in|init|"
    "inline|inner|interface|internal|is|lambda|lateinit|lazy|let|match|mod|module|mut|namespace|new|"
    "nil|none|None|not|null|object|open|operator|or|and|override|package|pass|private|protected|pub|"
    "public|raise|return|sealed|select|self|set|static|struct|super|suspend|switch|this|throw|throws|"
    "trait|true|True|False|try|type|typealias|typeof|undefined|use|val|var|void|when|where|while|"
    "with|yield"
)
HASH_COMMENT_EXT = {".py", ".sh", ".bash", ".zsh", ".rb", ".yml", ".yaml", ".toml", ".pl", ".r",
                    ".properties", ".cfg", ".ini", ".mk", ".dockerfile"}


def token_re(path: str):
    name = path.rsplit("/", 1)[-1].lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else name
    hash_comments = ext in HASH_COMMENT_EXT or name in ("makefile", "dockerfile")
    comment = r"#.*$" if hash_comments else r"//.*$|/\*.*?\*/|^\s*\*.*$"
    return re.compile(
        rf'(?P<str>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)|(?P<com>{comment})'
        rf"|(?P<ann>@[A-Za-z_][\w.]*)|(?P<kw>\b(?:{KEYWORDS})\b)|(?P<num>\b\d[\d_.]*\b)"
        rf"|(?P<typ>\b[A-Z][A-Za-z0-9_]*\b)"
    )


def highlight(code: str, pattern) -> str:
    out, pos = [], 0
    for m in pattern.finditer(code):
        out.append(html.escape(code[pos:m.start()]))
        out.append(f'<span class="tk-{m.lastgroup}">{html.escape(m.group())}</span>')
        pos = m.end()
    out.append(html.escape(code[pos:]))
    return "".join(out)


_ICON_PATHS = {
    "folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    "comment": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "commit": '<circle cx="12" cy="12" r="3"/><path d="M3 12h6M15 12h6"/>',
    "diff": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M9 10h6M12 7v6M9 17h6"/>',
    "pr": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7M6 9v12"/>',
    "chevron": '<path d="m6 9 6 6 6-6"/>',
    "sidebar": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
}


def icon(name: str, cls: str = "") -> str:
    return (
        f'<svg class="i {cls}" viewBox="0 0 24 24" aria-hidden="true">{_ICON_PATHS[name]}</svg>'
    )


ICONS = {f"i_{name}": icon(name) for name in _ICON_PATHS}


def avatar(name: str, cls: str) -> str:
    return f'<span class="avatar {cls}">{html.escape(name[0].upper())}</span>'


def label(kind: str) -> str:
    text, cls = LABELS[kind]
    return f'<span class="lbl {cls}">{text}</span>'


def comment_id(index: int) -> str:
    return f"c-{index}"


def inline_comment(index: int, kind: str, body: str, line: int) -> str:
    return f"""
<tr class="comment-row"><td colspan="3"><div class="comment-wrap">
  <div class="thread {'thread-blocking' if kind == 'blocking' else ''}" id="{comment_id(index)}">
    <div class="comment-head">
      {avatar(REVIEWER, 'av-bot')}
      <strong>{REVIEWER}</strong> <span class="muted hide-sm">commented on line</span> <code class="lineref">L{line}</code>
      {label(kind)}
      <button class="btn btn-sm right resolve">Resolve</button>
    </div>
    <div class="comment-body md">{md(body)}</div>
  </div>
</div></td></tr>"""


def file_id(path: str) -> str:
    return "f-" + re.sub(r"[^a-zA-Z0-9]", "-", path)


def render_file(f, comments_by_line) -> str:
    """comments_by_line maps (side, line) -> [(index, kind, body)], side being "new" or "old"."""
    pattern = token_re(f["path"])
    rows = []
    for kind, old, new, text in f["rows"]:
        if kind == "hunk":
            rows.append(f'<tr class="hunk"><td class="ln"></td><td class="ln"></td><td class="code">{html.escape(text)}</td></tr>')
            continue
        sign = {"add": "+", "del": "-", "ctx": " "}[kind]
        anchors = []
        if new is not None and ("new", new) in comments_by_line:
            anchors.append(("new", new))
        if kind == "del" and ("old", old) in comments_by_line:
            anchors.append(("old", old))
        rows.append(
            f'<tr class="row-{kind}{" has-comment" if anchors else ""}">'
            f'<td class="ln">{old or ""}</td><td class="ln">{new or ""}</td>'
            f'<td class="code"><span class="sign">{sign}</span>{highlight(text, pattern)}</td></tr>'
        )
        for key in anchors:
            where = key[1] if key[0] == "new" else f"{key[1]} (old)"
            for index, ckind, body in comments_by_line.pop(key):
                rows.append(inline_comment(index, ckind, body, where))
    for (side, line), items in sorted(comments_by_line.items()):  # anchors outside the diff hunks
        print(f"warning: {f['path']}:{line} ({side}) is not in the diff; comment placed at end of file",
              file=sys.stderr)
        for index, ckind, body in items:
            rows.append(inline_comment(index, ckind, body, line))
    if f["binary"]:
        rows.append('<tr class="hunk"><td class="ln"></td><td class="ln"></td><td class="code">Binary file not shown</td></tr>')
    total = f["add"] + f["del"]
    blocks = "".join(
        '<span class="blk blk-add"></span>' if i < round(5 * f["add"] / max(total, 1)) else '<span class="blk blk-del"></span>'
        for i in range(5)
    )
    n_comments = sum(1 for c in COMMENTS if c["file"] == f["path"])
    badge = f'<span class="file-comments">{icon("comment")} {n_comments}</span>' if n_comments else ""
    directory, _, name = f["path"].rpartition("/")
    directory = f"{directory}/" if directory else ""
    status = ""
    if f["status"] == "renamed":
        status = f'<span class="new-file" title="from {html.escape(f.get("renamed_from", ""))}">renamed</span>'
    elif f["status"]:
        status = f'<span class="new-file status-{f["status"]}">{f["status"]}</span>'
    return f"""
<div class="file" id="{file_id(f['path'])}">
  <div class="file-header">
    <button class="chev" aria-label="Toggle file">{icon("chevron")}</button>
    <span class="file-stat">{f['add'] + f['del']} {blocks}</span>
    <span class="file-path" title="{html.escape(f['path'])}"><span class="dir">{html.escape(directory)}</span>{html.escape(name)}</span>
    {status}
    {badge}
    <label class="viewed right"><input type="checkbox"> <span class="hide-sm">Viewed</span></label>
  </div>
  <div class="diff-scroll"><table class="diff">{''.join(rows)}</table></div>
</div>"""


def build_tree(files):
    root = {}
    for f in files:
        node = root
        *dirs, name = f["path"].split("/")
        for d in dirs:
            node = node.setdefault(d + "/", {})
        node[name] = f
    return root


def is_dir(node) -> bool:
    return isinstance(node, dict) and "rows" not in node


def compact(name, node):
    """Collapse single-child directory chains like GitHub does (a/b/c)."""
    while is_dir(node) and len(node) == 1:
        child_name, child = next(iter(node.items()))
        if not is_dir(child):
            break
        name, node = name + child_name, child
    return name, node


def render_tree(node, depth=0) -> str:
    out = []
    dirs = sorted(((k, v) for k, v in node.items() if is_dir(v)), key=lambda kv: kv[0])
    leaves = sorted(((k, v) for k, v in node.items() if not is_dir(v)), key=lambda kv: kv[0])
    pad = f'style="padding-left:{6 + depth * 10}px"'
    for name, child in dirs:
        name, child = compact(name, child)
        out.append(
            f'<div class="tree-dir"><button class="tree-row tree-folder" {pad}><span class="tw">{icon("chevron")}</span>'
            f'{icon("folder", "ico ico-folder")}<span class="tree-name" title="{html.escape(name.rstrip("/"))}">{html.escape(name.rstrip("/"))}</span></button>'
            f'<div class="tree-children">{render_tree(child, depth + 1)}</div></div>'
        )
    for name, f in leaves:
        n = sum(1 for c in COMMENTS if c["file"] == f["path"])
        count = f'<span class="tree-c">{icon("comment")}{n}</span>' if n else ""
        out.append(
            f'<a class="tree-row tree-file" href="#{file_id(f["path"])}" data-target="{file_id(f["path"])}" {pad}>'
            f'<span class="tw"></span>{icon("file", "ico ico-file")}<span class="tree-name" title="{html.escape(f["path"])}">{html.escape(name)}</span>'
            f'<span class="tree-stat">{count}'
            f'<span class="add">+{f["add"]}</span> <span class="del">−{f["del"]}</span></span></a>'
        )
    return "".join(out)




def load_diff(args) -> tuple[str, list, str, str, str]:
    """Return (diff, commits, author, base, head) from --diff or from git."""
    if args.diff:
        return Path(args.diff).read_text(), [], args.author or "", args.base or "base", args.head or "head"
    base = args.base or default_base(args.repo)
    head = args.head or "HEAD"
    diff = git(args.repo, "diff", "--no-color", "--no-ext-diff", "-M", f"{base}...{head}")
    log = git(args.repo, "log", "--no-merges", "--format=%h%x09%an%x09%s", f"{base}..{head}")
    commits = [line.split("\t", 2) for line in log.splitlines() if line.strip()]
    author = args.author or (commits[0][1] if commits else "")
    if head == "HEAD":
        head = git(args.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return diff, commits, author, base, head


def main():
    global COMMENTS, REVIEWER
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--review", required=True, help="review JSON (title, comments)")
    parser.add_argument("--out", required=True, help="output HTML file")
    parser.add_argument("--repo", default=".", help="git repository (default: cwd)")
    parser.add_argument("--base", help="base branch (default: origin/HEAD, else main/master/develop)")
    parser.add_argument("--head", help="branch or ref to review (default: HEAD)")
    parser.add_argument("--diff", help="use this unified diff file instead of running git")
    parser.add_argument("--author", help="override the PR author shown in the header")
    parser.add_argument("--exclude", action="append", default=[],
                        help="glob of files to leave out of the page (repeatable), e.g. '*.lock'")
    args = parser.parse_args()

    review = json.loads(Path(args.review).read_text())
    REVIEWER = review.get("reviewer", REVIEWER)
    diff, commits, author, base, head = load_diff(args)
    files = [f for f in parse_diff(diff) if not any(fnmatch.fnmatch(f["path"], g) for g in args.exclude)]
    if not files:
        sys.exit("The diff is empty; nothing to render.")

    paths = {f["path"] for f in files}
    COMMENTS = []
    for c in review.get("comments", []):
        kind = c.get("label", "suggestion").lower()
        if kind not in LABELS:
            print(f"warning: unknown label {kind!r}, using 'suggestion'", file=sys.stderr)
            kind = "suggestion"
        if c["file"] not in paths:
            print(f"warning: {c['file']} is not in the diff; comment dropped", file=sys.stderr)
            continue
        COMMENTS.append({"file": c["file"], "line": int(c["line"]), "side": c.get("side", "new"),
                         "label": kind, "body": c["body"]})
    COMMENTS.sort(key=lambda c: list(LABELS).index(c["label"]))

    rendered = []
    for f in files:
        by_line = {}
        for index, c in enumerate(COMMENTS):
            if c["file"] == f["path"]:
                by_line.setdefault((c["side"], c["line"]), []).append((index, c["label"], c["body"]))
        rendered.append(render_file(f, by_line))

    commit_items = "".join(
        f'<div class="timeline-item">{avatar(name, "av-author")}<span class="commit-msg">{html.escape(msg)}</span>'
        f'<code class="sha">{sha}</code></div>'
        for sha, name, msg in commits
    ) or '<p class="muted">No commit information (rendered from a diff file).</p>'

    page = TEMPLATE.format(
        title=html.escape(review.get("title") or f"Review of {head}"),
        branch=html.escape(head.removeprefix("origin/")),
        base=html.escape(base),
        author=html.escape(author or "Someone"),
        n_commits=len(commits),
        commits_label=f"{len(commits)} commit" + ("" if len(commits) == 1 else "s"),
        n_files=len(files),
        n_comments=len(COMMENTS),
        adds=sum(f["add"] for f in files),
        dels=sum(f["del"] for f in files),
        commit_items=commit_items,
        files="".join(rendered),
        tree=render_tree(build_tree(files)),
        **ICONS,
    )
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({len(files)} files, {len(COMMENTS)} comments)")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Review</title>
<style>
:root {{
  --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --border:#30363d; --border2:#21262d;
  --fg:#e6edf3; --muted:#8d96a0; --link:#4493f8; --green:#3fb950; --red:#f85149;
  --add-bg:rgba(46,160,67,.15); --add-ln:rgba(63,185,80,.3); --del-bg:rgba(248,81,73,.1); --del-ln:rgba(248,81,73,.3);
  --hunk-bg:rgba(56,139,253,.1); --orange:#db6d28; --sticky:56px;
  --neutral:rgba(110,118,129,.4); --btn-hover:#30363d; --folder:#54aeff; --accent-bg:rgba(56,139,253,.15);
  --tk-kw:#ff7b72; --tk-str:#a5d6ff; --tk-com:#8b949e; --tk-ann:#d2a8ff; --tk-num:#79c0ff; --tk-typ:#ffa657;
  --lbl-red:#ff7b72; --lbl-blue:#79c0ff; --lbl-purple:#d2a8ff; --lbl-orange:#ffa657; --lbl-green:#7ee787;
  color-scheme:dark;
}}
@media (prefers-color-scheme: light) {{
  :root {{
    --bg:#ffffff; --bg2:#f6f8fa; --bg3:#f3f4f6; --border:#d1d9e0; --border2:#e6eaef;
    --fg:#1f2328; --muted:#59636e; --link:#0969da; --green:#1a7f37; --red:#d1242f;
    --add-bg:#e6ffec; --add-ln:#ccffd8; --del-bg:#ffebe9; --del-ln:#ffd7d5;
    --hunk-bg:#ddf4ff; --orange:#bc4c00;
    --neutral:rgba(175,184,193,.3); --btn-hover:#eaeef2; --folder:#54aeff; --accent-bg:rgba(9,105,218,.1);
    --tk-kw:#cf222e; --tk-str:#0a3069; --tk-com:#6e7781; --tk-ann:#8250df; --tk-num:#0550ae; --tk-typ:#953800;
    --lbl-red:#d1242f; --lbl-blue:#0969da; --lbl-purple:#8250df; --lbl-orange:#bc4c00; --lbl-green:#1a7f37;
    color-scheme:light;
  }}
}}
* {{ box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif; -webkit-text-size-adjust:100%; }}
a {{ color:var(--link); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
button {{ font:inherit; color:inherit; }}
code {{ font:12px ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; background:var(--neutral); padding:.15em .4em; border-radius:6px; }}
.i {{ width:16px; height:16px; flex:none; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; vertical-align:-3px; }}
.ico {{ width:15px; height:15px; }}
.ico-folder {{ color:var(--folder); fill:rgba(84,174,255,.25); }}
.ico-file {{ color:var(--muted); }}
.tw .i, .chev .i {{ width:14px; height:14px; vertical-align:middle; }}
.state .i, .tab .i {{ vertical-align:-3px; }}
.tab .i {{ color:var(--muted); }} .tab.active .i {{ color:var(--fg); }}
.btn .i {{ width:14px; height:14px; vertical-align:-2px; }}
.file-comments, .tree-c {{ display:inline-flex; align-items:center; gap:3px; }}
.file-comments .i, .tree-c .i {{ width:13px; height:13px; }}
.muted {{ color:var(--muted); }} .right {{ margin-left:auto; }}
.wrap {{ max-width:1280px; margin:0 auto; padding:24px; }}
h1.pr-title {{ font-size:28px; font-weight:400; margin:0 0 8px; line-height:1.25; }}
.pr-meta {{ display:flex; align-items:center; gap:8px; color:var(--muted); flex-wrap:wrap; }}
.state {{ background:#238636; color:#fff; border-radius:2em; padding:4px 12px; font-weight:500; white-space:nowrap; display:inline-flex; align-items:center; gap:6px; }}
.branch {{ background:var(--accent-bg); color:var(--link); padding:2px 6px; border-radius:6px; font:12px ui-monospace,Menlo,monospace; word-break:break-all; }}
.tabbar {{ position:sticky; top:0; z-index:5; background:var(--bg); display:flex; align-items:flex-end; gap:8px; border-bottom:1px solid var(--border); margin-top:16px; }}
.tabs {{ display:flex; gap:4px; overflow-x:auto; scrollbar-width:none; }}
.tab {{ padding:8px 14px; cursor:pointer; color:var(--muted); border:1px solid transparent; border-bottom:none; border-radius:6px 6px 0 0; margin-bottom:-1px; background:none; display:flex; gap:6px; align-items:center; white-space:nowrap; }}
.tab.active {{ color:var(--fg); background:var(--bg); border-color:var(--border); }}
.counter {{ background:var(--neutral); border-radius:2em; padding:0 6px; font-size:12px; }}
.diffstat {{ margin-left:auto; align-self:center; font-size:12px; white-space:nowrap; padding-bottom:4px; }}
.add {{ color:var(--green); }} .del {{ color:var(--red); }}
.panel {{ display:none; padding-top:24px; }} .panel.active {{ display:block; }}
/* conversation */
.timeline {{ position:relative; }}
.timeline::before {{ content:""; position:absolute; left:19px; top:0; bottom:0; width:2px; background:var(--border2); }}
.timeline-item {{ position:relative; display:flex; align-items:center; gap:10px; padding:8px 0 8px 8px; font-size:13px; }}
.timeline-item .avatar {{ width:24px; height:24px; font-size:11px; }}
.commit-msg {{ color:var(--fg); min-width:0; }} .sha {{ margin-left:auto; background:none; color:var(--muted); }}
.avatar {{ width:40px; height:40px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; font-weight:600; color:#fff; flex:none; position:relative; z-index:1; }}
.av-author {{ background:linear-gradient(135deg,#1f6feb,#8957e5); }}
.av-bot {{ background:linear-gradient(135deg,#d97757,#b04a2f); }}
.comment-head .avatar {{ width:20px; height:20px; font-size:10px; }}
.md {{ padding:16px; overflow-wrap:anywhere; }} .md p {{ margin:0 0 12px; }} .md p:last-child {{ margin:0; }}
.md h3 {{ font-size:15px; margin:18px 0 6px; padding-bottom:4px; border-bottom:1px solid var(--border2); }}
.md ul {{ margin:0 0 8px; padding-left:24px; }} .md li {{ margin:4px 0; }}
.lbl {{ border-radius:2em; padding:0 8px; font-size:12px; font-weight:500; border:1px solid; line-height:20px; white-space:nowrap; }}
.lbl-red {{ color:var(--lbl-red); border-color:rgba(248,81,73,.6); background:rgba(248,81,73,.15); }}
.lbl-blue {{ color:var(--lbl-blue); border-color:rgba(56,139,253,.6); background:rgba(56,139,253,.15); }}
.lbl-purple {{ color:var(--lbl-purple); border-color:rgba(163,113,247,.6); background:rgba(163,113,247,.15); }}
.lbl-orange {{ color:var(--lbl-orange); border-color:rgba(219,109,40,.6); background:rgba(219,109,40,.15); }}
.lbl-gray {{ color:var(--muted); border-color:var(--border); background:var(--neutral); }}
.lbl-green {{ color:var(--lbl-green); border-color:rgba(63,185,80,.6); background:rgba(63,185,80,.15); }}
.btn {{ background:var(--bg3); border:1px solid var(--border); border-radius:6px; padding:5px 14px; font-weight:500; cursor:pointer; white-space:nowrap; }}
.btn:hover {{ background:var(--btn-hover); }}
.btn-sm {{ padding:2px 10px; font-size:12px; }}
/* files changed */
.files-layout > * {{ min-width:0; }}
.files-layout {{ display:grid; grid-template-columns:340px minmax(0,1fr); gap:16px; align-items:start; }}
.tree {{ position:sticky; top:calc(var(--sticky) + 8px); border:1px solid var(--border); border-radius:6px; padding:6px; max-height:calc(100vh - var(--sticky) - 24px); overflow:auto; font-size:13px; }}
.tree-row {{ display:flex; align-items:center; gap:4px; width:100%; padding:4px 8px; border-radius:6px; color:var(--fg); background:none; border:none; cursor:pointer; text-align:left; }}
.tree-row:hover {{ background:var(--bg3); text-decoration:none; }}
.tree-file.active {{ background:var(--accent-bg); box-shadow:inset 2px 0 0 var(--link); }}
.tw {{ width:14px; flex:none; color:var(--muted); display:inline-flex; transition:transform .15s; }}
.tree-dir.closed > .tree-folder .tw {{ transform:rotate(-90deg); }}
.tree-dir.closed > .tree-children {{ display:none; }}
.tree-folder .tree-name {{ color:var(--muted); }}
.tree-name {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; flex:1; }}
.tree-stat {{ font-size:11px; white-space:nowrap; display:flex; gap:4px; }} .tree-c {{ color:var(--muted); }}
.file-toolbar {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:12px; color:var(--muted); font-size:13px; flex-wrap:wrap; }}
.tree-toggle {{ display:none; }}
.file {{ border:1px solid var(--border); border-radius:6px; margin-bottom:16px; scroll-margin-top:calc(var(--sticky) + 8px); }}
.file-header {{ position:sticky; top:var(--sticky); z-index:3; background:var(--bg2); padding:8px 12px; display:flex; gap:10px; align-items:center; border-bottom:1px solid var(--border); border-radius:6px 6px 0 0; font-size:12px; min-width:0; }}
.file.collapsed .file-header {{ border-bottom:none; border-radius:6px; }}
.file-path {{ font:12px ui-monospace,Menlo,monospace; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; direction:rtl; text-align:left; }}
.file-path .dir {{ color:var(--muted); }}
.chev {{ color:var(--muted); cursor:pointer; background:none; border:none; padding:0 2px; display:inline-flex; transition:transform .15s; }}
.file.collapsed .diff-scroll {{ display:none; }} .file.collapsed .chev {{ transform:rotate(-90deg); }}
.file.flash {{ animation:flash 1.2s ease-out; }}
@keyframes flash {{ from {{ box-shadow:0 0 0 3px rgba(56,139,253,.6); }} to {{ box-shadow:0 0 0 3px rgba(56,139,253,0); }} }}
.file-stat {{ font:12px ui-monospace,Menlo,monospace; display:flex; align-items:center; gap:1px; flex:none; }}
.blk {{ display:inline-block; width:8px; height:8px; margin-left:1px; }} .blk-add {{ background:var(--green); }} .blk-del {{ background:var(--red); }}
.new-file {{ color:var(--green); border:1px solid rgba(63,185,80,.5); border-radius:2em; padding:0 6px; flex:none; }}
.file-comments {{ color:var(--muted); flex:none; }}
.viewed {{ border:1px solid var(--border); border-radius:6px; padding:2px 8px; color:var(--muted); cursor:pointer; flex:none; display:flex; gap:4px; align-items:center; }}
.diff-scroll {{ overflow-x:auto; container-type:inline-size; overscroll-behavior-x:contain; }}
table.diff {{ width:max-content; min-width:100%; border-collapse:collapse; font:12px/20px ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; }}
.diff td {{ padding:0 10px; vertical-align:top; }}
.diff td.ln {{ width:1%; min-width:50px; white-space:nowrap; color:var(--muted); text-align:right; user-select:none; opacity:.8; }}
.diff td.code {{ white-space:pre; padding-right:24px; }}
.sign {{ display:inline-block; width:14px; user-select:none; color:var(--muted); }}
.row-add td {{ background:var(--add-bg); }} .row-add td.ln {{ background:var(--add-ln); }}
.row-del td {{ background:var(--del-bg); }} .row-del td.ln {{ background:var(--del-ln); }}
.row-add .sign {{ color:var(--green); }} .row-del .sign {{ color:var(--red); }}
.hunk td {{ background:var(--hunk-bg); color:var(--muted); padding:4px 10px; white-space:pre; }}
.has-comment td.ln:nth-child(2) {{ box-shadow:inset -3px 0 0 #d29922; }}
.tk-kw {{ color:var(--tk-kw); }} .tk-str {{ color:var(--tk-str); }} .tk-com {{ color:var(--tk-com); font-style:italic; }}
.tk-ann {{ color:var(--tk-ann); }} .tk-num {{ color:var(--tk-num); }} .tk-typ {{ color:var(--tk-typ); }}
.comment-wrap {{ position:sticky; left:0; width:100cqw; }}
tr.comment-row td {{ padding:0; background:var(--bg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; white-space:normal; word-break:normal; }}
.thread {{ margin:8px 16px 8px 116px; border:1px solid var(--border); border-radius:6px; background:var(--bg); max-width:860px; scroll-margin-top:calc(var(--sticky) + 56px); }}
.thread-blocking {{ border-color:rgba(248,81,73,.6); box-shadow:0 0 0 1px rgba(248,81,73,.2); }}
.thread.flash {{ animation:flash 1.2s ease-out; }}
.comment-head {{ display:flex; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid var(--border2); background:var(--bg2); font-size:13px; flex-wrap:wrap; border-radius:6px 6px 0 0; }}
.thread.resolved .comment-head {{ border-bottom:none; border-radius:6px; opacity:.7; }}
.thread.resolved .comment-body {{ display:none; }}
.lineref {{ font-size:11px; }}
.comment-body.md {{ padding:12px 16px; }}
.md-code {{ margin:0 0 12px; padding:10px 12px; background:var(--bg2); border:1px solid var(--border2); border-radius:6px; overflow-x:auto; font:12px/1.5 ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; white-space:pre; }}
.status-deleted {{ color:var(--red); border-color:var(--red); }}
/* tablet */
@media (max-width:1012px) {{
  .files-layout {{ grid-template-columns:minmax(0,1fr); }}
  .tree-toggle {{ display:inline-block; }}
  .tree {{ display:none; position:static; max-height:50vh; margin-bottom:12px; }}
  .files-layout.tree-open .tree {{ display:block; }}
  .thread {{ margin-left:16px; }}
}}
/* phone */
@media (max-width:640px) {{
  :root {{ --sticky:48px; }}
  .wrap {{ padding:12px; }}
  h1.pr-title {{ font-size:20px; }}
  .pr-meta {{ font-size:13px; }}
  .tab {{ padding:6px 10px; font-size:13px; }}
  .tab .tab-text {{ display:none; }}
  .timeline::before {{ left:15px; }}
  .md {{ padding:12px; }}
  .hide-sm {{ display:none; }}
  .diff td.ln {{ min-width:36px; }}
  .diff td {{ padding:0 6px; }}
  .diff td.ln:first-child {{ display:none; }}
  .thread {{ margin:6px; }}
  tr.comment-row td {{ padding:0; }}
  .comment-body.md {{ padding:10px 12px; font-size:13px; }}
  .file-header {{ padding:6px 8px; gap:6px; }}
  .file-stat .blk {{ display:none; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <h1 class="pr-title">{title}</h1>
  <div class="pr-meta">
    <span class="state">{i_pr} Open</span>
    <span><strong style="color:var(--fg)">{author}</strong> wants to merge {commits_label} into <span class="branch">{base}</span> from <span class="branch">{branch}</span></span>
  </div>

  <div class="tabbar">
    <div class="tabs">
      <button class="tab" data-tab="commits">{i_commit} <span class="tab-text">Commits</span> <span class="counter">{n_commits}</span></button>
      <button class="tab active" data-tab="files">{i_diff} <span class="tab-text">Files changed</span> <span class="counter">{n_files}</span></button>
    </div>
    <span class="diffstat"><span class="add">+{adds}</span> <span class="del">−{dels}</span></span>
  </div>

  <section class="panel" id="commits">
    <div class="timeline">{commit_items}</div>
  </section>

  <section class="panel active" id="files">
    <div class="file-toolbar">
      <span><button class="btn btn-sm tree-toggle" id="tree-toggle">{i_sidebar} Files</button> {n_files} files · {n_comments} comments</span>
      <button class="btn btn-sm" id="toggle-all">Collapse all</button>
    </div>
    <div class="files-layout">
      <nav class="tree" aria-label="Changed files">{tree}</nav>
      <div>
        {files}
      </div>
    </div>
  </section>
</div>

<script>
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const layout = $('.files-layout');
const syncSticky = () => document.documentElement.style.setProperty('--sticky', $('.tabbar').offsetHeight + 'px');
syncSticky();
addEventListener('resize', syncSticky);

function showTab(name) {{
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.panel').forEach(p => p.classList.toggle('active', p.id === name));
}}
$$('.tab').forEach(t => t.addEventListener('click', () => showTab(t.dataset.tab)));

function flash(el) {{
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}}

function scrollToTarget(id) {{
  const el = document.getElementById(id);
  if (!el) return;
  showTab('files');
  const file = el.closest('.file');
  if (file) file.classList.remove('collapsed');
  const thread = el.closest('.thread');
  if (thread) thread.classList.remove('resolved');
  if (matchMedia('(max-width:1012px)').matches) layout.classList.remove('tree-open');
  requestAnimationFrame(() => {{
    el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    history.replaceState(null, '', '#' + id);
    flash(el);
  }});
}}
$$('a[data-target]').forEach(a => a.addEventListener('click', e => {{
  e.preventDefault();
  scrollToTarget(a.dataset.target);
}}));

$$('.tree-folder').forEach(b => b.addEventListener('click', () => b.parentElement.classList.toggle('closed')));
$('#tree-toggle').addEventListener('click', () => layout.classList.toggle('tree-open'));

$$('.file .chev').forEach(c => c.addEventListener('click', () => c.closest('.file').classList.toggle('collapsed')));
$('#toggle-all').addEventListener('click', e => {{
  const collapse = e.target.textContent === 'Collapse all';
  $$('.file').forEach(f => f.classList.toggle('collapsed', collapse));
  e.target.textContent = collapse ? 'Expand all' : 'Collapse all';
}});
$$('.viewed input').forEach(cb => cb.addEventListener('change', () => cb.closest('.file').classList.toggle('collapsed', cb.checked)));
$$('.resolve').forEach(b => b.addEventListener('click', () => {{
  const t = b.closest('.thread');
  t.classList.toggle('resolved');
  b.textContent = t.classList.contains('resolved') ? 'Unresolve' : 'Resolve';
}}));

// highlight the file currently in view in the tree
const treeLinks = new Map($$('.tree-file').map(a => [a.dataset.target, a]));
const observer = new IntersectionObserver(entries => {{
  entries.filter(e => e.isIntersecting).forEach(e => {{
    treeLinks.forEach(a => a.classList.remove('active'));
    treeLinks.get(e.target.id)?.classList.add('active');
  }});
}}, {{ rootMargin: '-80px 0px -70% 0px' }});
$$('.file').forEach(f => observer.observe(f));

if (location.hash.length > 1) scrollToTarget(location.hash.slice(1));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
