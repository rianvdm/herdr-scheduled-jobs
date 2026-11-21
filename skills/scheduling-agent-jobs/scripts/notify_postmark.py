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
REPLY_MAX = 65536


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
        except OSError:
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
    if run.get("slug"):
        message["Tag"] = run["slug"]
    send(find_token(args.env_file, args.token_var), message)
    print("notify_postmark: sent")


if __name__ == "__main__":
    main()
