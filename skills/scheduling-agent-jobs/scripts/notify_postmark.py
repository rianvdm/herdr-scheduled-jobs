#!/usr/bin/python3
"""Email a scheduled job's result through Postmark.

jobs.py runs this as a job's notify hook and passes the finished run on stdin as JSON
(payload version 1). On success it sends the job's output file; on any other result, the
error and the end of the log. Standard library only, for the macOS system Python.

  "notify": {"command": ["/usr/bin/python3",
      "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
      "--from", "reports@example.com", "--to", "me@example.com",
      "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]}

The token is a Postmark server token, read from --env-file (or from the environment when no
file is given). It is never printed.
"""
import argparse
import html
import json
import os
import http.client
import re
import sys
import urllib.error
import urllib.request

API_URL = "https://api.postmarkapp.com/email"
LOCAL_URL = re.compile(r"^http://(127\.0\.0\.1|localhost)(:[0-9]+)?/")
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
SUBJECT_MAX = 200
BODY_MAX = 1_000_000
HTML_MAX = 100_000  # longer reports go out as plain text only
LINE_MAX = 10_000  # a longer line is escaped without inline formatting
REPLY_MAX = 65536

HEADING = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
BULLET = re.compile(r"^( *)[-*]\s+(.*)$")
RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]+)\]\(((?:[^()\s`\x00]|\([^()\s`\x00]*\))+)\)")
BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
STAR_EM = re.compile(r"\*(?!\s)(.+?)(?<!\s)\*")
UNDERSCORE_EM = re.compile(r"(?<![A-Za-z0-9_@])_(?!\s)(.+?)(?<!\s)_(?![A-Za-z0-9_])")
SAFE_URL = re.compile(r"^(https?://|mailto:)", re.I)
HOLE = re.compile("\x00(\\d+)\x00")

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
HEADING_STYLE = {1: "font-size:24px;margin:24px 0 12px", 2: "font-size:20px;margin:22px 0 10px",
                 3: "font-size:17px;margin:18px 0 8px", 4: "font-size:15px;margin:16px 0 6px",
                 5: "font-size:14px;margin:14px 0 6px", 6: "font-size:13px;margin:14px 0 6px"}
CODE_STYLE = f"font-family:{MONO};font-size:13px;background:#f3f4f6;padding:1px 4px;border-radius:3px"
PRE_STYLE = (f"font-family:{MONO};font-size:13px;background:#f3f4f6;padding:10px 12px;"
             "border-radius:4px;overflow-x:auto;margin:12px 0")


def inline_html(text):
    """Escape one line of Markdown text, then add code, links, bold, and italics."""
    if len(text) > LINE_MAX:  # the emphasis patterns are slow on huge lines
        return html.escape(text.replace("\x00", ""), quote=True)
    holes = []

    def hide(markup):
        holes.append(markup)
        return f"\x00{len(holes) - 1}\x00"

    def emphasis(t):
        t = BOLD.sub(r"<strong>\1</strong>", t)
        t = STAR_EM.sub(r"<em>\1</em>", t)
        return UNDERSCORE_EM.sub(r"<em>\1</em>", t)

    def link(m):
        label, url = m.group(1), m.group(2)
        if not SAFE_URL.match(html.unescape(url)):
            return f"{emphasis(label)} ({url})"
        return hide(f'<a href="{url}" style="color:#1a56db">{emphasis(label)}</a>')

    t = html.escape(text.replace("\x00", ""), quote=True)
    t = CODE_SPAN.sub(lambda m: hide(f'<code style="{CODE_STYLE}">{m.group(1)}</code>'), t)
    t = emphasis(LINK.sub(link, t))
    while HOLE.search(t):  # links can hold code spans, so unwrap until none are left
        t = HOLE.sub(lambda m: holes[int(m.group(1))], t)
    return t


def list_html(items):
    """items are (level, text); level 1 nests one <ul> under the item before it."""
    out, open_li, nested = ['<ul style="margin:8px 0;padding-left:24px">'], False, False
    for level, text in items:
        if level == 0:
            out.append("</ul></li>" if nested else "</li>" if open_li else "")
            nested = False
            out.append(f'<li style="margin:4px 0">{inline_html(text)}')
            open_li = True
        else:
            if not nested:
                if not open_li:
                    out.append('<li style="margin:4px 0">')
                    open_li = True
                out.append('<ul style="margin:4px 0;padding-left:24px">')
                nested = True
            out.append(f'<li style="margin:4px 0">{inline_html(text)}</li>')
    out.append("</ul></li>" if nested else "</li>" if open_li else "")
    out.append("</ul>")
    return "".join(out)


def markdown_to_html(text):
    """A small Markdown subset (headings, bullets, code, rules, emphasis, links) as an HTML page.

    Everything is escaped first, so the report can't add markup. Unknown syntax stays text."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.lstrip().startswith("```"):
            code, i = [], i + 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # the closing fence, if there is one
            blocks.append(f'<pre style="{PRE_STYLE}"><code>{html.escape(chr(10).join(code))}</code></pre>')
        elif RULE.match(line):
            blocks.append('<hr style="border:0;border-top:1px solid #d1d5db;margin:20px 0">')
            i += 1
        elif HEADING.match(line):
            level, body = HEADING.match(line).groups()
            n = len(level)
            blocks.append(f'<h{n} style="{HEADING_STYLE[n]};line-height:1.3">{inline_html(body)}</h{n}>')
            i += 1
        elif BULLET.match(line):
            items = []
            while i < len(lines) and BULLET.match(lines[i]) and not RULE.match(lines[i]):
                indent, body = BULLET.match(lines[i]).groups()
                items.append((1 if len(indent) >= 2 else 0, body))
                i += 1
            blocks.append(list_html(items))
        else:
            para = []
            while (i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith("```")
                   and not RULE.match(lines[i]) and not HEADING.match(lines[i]) and not BULLET.match(lines[i])):
                para.append(lines[i].strip())
                i += 1
            blocks.append(f'<p style="margin:10px 0">{inline_html(" ".join(para))}</p>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
            f'<body style="margin:0;padding:16px;background:#ffffff">'
            f'<div style="font-family:{FONT};font-size:15px;line-height:1.5;color:#111827;max-width:680px">'
            + "\n".join(blocks) + "</div></body></html>")


def fail(message, token=None):
    text = str(message)
    if token:
        text = text.replace(token, "[token]")
    print(f"notify_postmark: {text}", file=sys.stderr)
    sys.exit(1)


def parse_value(raw):
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def read_env_file(p):
    """KEY=value lines; export, quotes, and # comments allowed; the last assignment wins."""
    values = {}
    with open(os.path.expanduser(p)) as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = LINE.match(line)
            if m:
                values[m.group(1)] = parse_value(m.group(2))
    return values


def find_token(env_file, var):
    if env_file:
        try:
            value = read_env_file(env_file).get(var)
        except (OSError, ValueError):
            fail(f"can't read {env_file}")
        where = env_file
    else:
        value, where = os.environ.get(var), "the environment"
    if not value:
        fail(f"{var} isn't set in {where}")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        fail(f"{var} has spaces or control characters")
    return value


def api_url():
    """Postmark's endpoint, or POSTMARK_API_URL for tests, which may only point at this Mac."""
    override = os.environ.get("POSTMARK_API_URL")
    if override is None:
        return API_URL
    if not LOCAL_URL.match(override):
        fail("POSTMARK_API_URL may only point at a local test server")
    return override


def subject(run):
    text = f'{run.get("title") or "Scheduled job"}: {run.get("message") or ""}'
    return " ".join(text.split())[:SUBJECT_MAX].rstrip(": ")


def markdown_report(run):
    """True when the email body comes from a Markdown output file."""
    out = run.get("expectedFile")
    return run.get("status") == "success" and bool(out) and str(out).lower().endswith(".md")


def text_body(run):
    if run.get("status") == "success":
        out = run.get("expectedFile")
        if not out:
            return run.get("resultText") or run.get("message") or "Finished."
        try:
            with open(out, errors="replace") as f:
                text = f.read(BODY_MAX + 1)
        except OSError:
            fail("can't read the job's output file")
        return text if len(text) <= BODY_MAX else text[:BODY_MAX] + "\n\n[truncated]"
    parts = [run.get("error") or run.get("message") or run.get("status") or "failed", "",
             f'Log: {run.get("logPath")}']
    if run.get("resumeHint"):
        parts.append(run["resumeHint"])
    if run.get("logTail"):
        parts += ["", "Last lines of the log:", "", run["logTail"]]
    return "\n".join(parts)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib then raises HTTPError, so the token never goes to another URL


def send(token, message):
    url = api_url()
    try:
        req = urllib.request.Request(url, data=json.dumps(message).encode(), method="POST", headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "X-Postmark-Server-Token": token})
        try:
            response = urllib.request.build_opener(NoRedirect).open(req, timeout=20)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            status, raw = response.code, response.read(REPLY_MAX)
    except (OSError, ValueError, http.client.HTTPException) as e:  # URLError is an OSError
        fail(f"couldn't reach Postmark ({type(e).__name__})", token)
    try:
        reply = json.loads(raw)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        fail(f"Postmark returned HTTP {status} without a JSON object")
    code = reply.get("ErrorCode")
    if status == 200 and type(code) is int and code == 0:
        return
    fail(f'Postmark error {code} (HTTP {status}): {reply.get("Message")}', token)


def main():
    p = argparse.ArgumentParser(description="Email a scheduled job's result through Postmark.")
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--to", action="append", required=True)
    p.add_argument("--token-var", required=True, help="name of the variable that holds the server token")
    p.add_argument("--env-file", help="KEY=value file to read the token from (default: the environment)")
    p.add_argument("--message-stream", default="outbound")
    args = p.parse_args()
    try:
        run = json.loads(sys.stdin.read())
    except ValueError:
        fail("the payload on stdin isn't JSON")
    if not isinstance(run, dict):
        fail("the payload on stdin isn't a JSON object")
    if type(run.get("version")) is not int or run["version"] != 1:
        fail("unsupported payload version")
    message = {"From": args.sender, "To": ", ".join(args.to), "Subject": subject(run),
               "TextBody": text_body(run), "MessageStream": args.message_stream}
    if markdown_report(run) and len(message["TextBody"]) <= HTML_MAX:
        message["HtmlBody"] = markdown_to_html(message["TextBody"])
    if run.get("slug"):
        message["Tag"] = run["slug"]
    send(find_token(args.env_file, args.token_var), message)
    print("notify_postmark: sent")


if __name__ == "__main__":
    main()
