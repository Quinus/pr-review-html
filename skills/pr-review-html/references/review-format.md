# Review JSON format

```json
{
  "title": "string, optional — page title; defaults to 'Review of <branch>'",
  "reviewer": "string, optional — name on each comment; defaults to 'agent-review'",
  "comments": [
    {
      "file": "path relative to repo root, exactly as in the diff (new path for renames)",
      "line": 42,
      "side": "new | old — optional, default new",
      "label": "blocking | suggestion | question | design | praise | nit — default suggestion",
      "body": "markdown subset, see below"
    }
  ]
}
```

## Anchoring rules

- `side: "new"` + a new-file line number anchors on an added (`+`) or context line.
- `side: "old"` + an old-file line number anchors on a removed (`-`) line.
- Lines outside every hunk are rendered at the end of the file's diff with a warning — pick a
  nearby line that is in the hunk instead.
- Comments for files not in the diff (or excluded with `--exclude`) are dropped with a warning.
- Several comments may share a line; they render in label order (blocking first).

## Body markdown

| Syntax | Result |
|---|---|
| blank line | new paragraph |
| `- item` / `* item` | bullet list |
| `# Heading` (any level) | small heading |
| `**bold**` | bold |
| `` `code` `` | inline code |
| `[text](https://…)` | link |
| ```` ``` ```` fenced block | preformatted code, scrolls horizontally |

HTML in bodies is escaped, so angle brackets in generics (`List<String>`) are safe.
