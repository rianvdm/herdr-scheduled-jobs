#!/usr/bin/python3
"""Tests for notify_postmark.py. A local HTTP server stands in for Postmark and the token is
a dummy, so nothing is sent.
Run: /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py"""
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = os.path.join(HERE, "..", "scripts", "notify_postmark.py")
TOKEN = "dummy-token-5f1c"
OK = (200, {"ErrorCode": 0, "Message": "OK", "MessageID": "msg-1"}, {})


def load_sender():
    spec = importlib.util.spec_from_file_location("notify_postmark", SENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePostmark(http.server.BaseHTTPRequestHandler):
    reply = OK
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakePostmark.received.append((self.headers, body))  # self.headers looks names up case-insensitively
        self.answer(*FakePostmark.reply)

    def do_GET(self):  # only reached if a redirect were followed
        FakePostmark.received.append((self.headers, None))
        self.answer(*OK)

    def answer(self, code, reply, headers):
        data = (reply if isinstance(reply, str) else json.dumps(reply)).encode()
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Sender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakePostmark)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/email"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakePostmark.received = []
        FakePostmark.reply = OK
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w") as f:
            f.write(f'# keys\n\nOTHER=1\nexport POSTMARK_TOKEN="{TOKEN}" # server token\n')
        self.out = os.path.join(self.tmp, "report.md")
        with open(self.out, "w") as f:
            f.write("# Report\n\n3 new items\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sender(self, payload, *extra, env_file=True, raw=None, token_env=None, api_url=None):
        args = ["/usr/bin/python3", SENDER, "--from", "jobs@example.com", "--to", "me@example.com",
                "--token-var", "POSTMARK_TOKEN", *extra]
        if env_file:
            args += ["--env-file", self.env_file]
        env = dict(os.environ, POSTMARK_API_URL=api_url or self.url, no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        env.pop("POSTMARK_TOKEN", None)
        if token_env:
            env["POSTMARK_TOKEN"] = token_env
        r = subprocess.run(args, input=raw if raw is not None else json.dumps(payload), env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertFalse(TOKEN in r.stdout or TOKEN in r.stderr, "the token appeared in the sender's output")
        return r

    def success(self, **over):
        p = {"version": 1, "slug": "report", "status": "success", "title": "Report ready",
             "message": "3 new items.", "expectedFile": self.out, "error": None,
             "logPath": "/tmp/report.log", "logTail": "line", "resumeHint": None, "resultText": None}
        p.update(over)
        return p

    def test_success_sends_the_output_file(self):
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 0, r.stderr)
        headers, body = FakePostmark.received[0]
        self.assertTrue(headers.get("X-Postmark-Server-Token") == TOKEN, "wrong or missing token header")
        self.assertEqual((body["From"], body["To"], body["Subject"]),
                         ("jobs@example.com", "me@example.com", "Report ready: 3 new items."))
        self.assertEqual(body["TextBody"], "# Report\n\n3 new items\n")
        self.assertEqual((body["MessageStream"], body["Tag"]), ("outbound", "report"))
        self.assertEqual(r.stdout.strip(), "notify_postmark: sent")

    def test_success_without_output_file(self):
        self.assertEqual(self.run_sender(self.success(expectedFile=None, resultText="Summary.")).returncode, 0)
        self.assertEqual(FakePostmark.received[-1][1]["TextBody"], "Summary.")
        self.assertEqual(self.run_sender(self.success(expectedFile=None)).returncode, 0)
        self.assertEqual(FakePostmark.received[-1][1]["TextBody"], "3 new items.")

    def test_failure_sends_error_log_and_tail(self):
        r = self.run_sender(self.success(status="failed", title="Report failed", message="Exit code 3.",
                                         error="exit code 3", logTail="boom\nlast line",
                                         resumeHint="cd /w && claude --resume s-1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = FakePostmark.received[0][1]["TextBody"]
        for part in ("exit code 3", "Log: /tmp/report.log", "claude --resume s-1", "boom\nlast line"):
            self.assertIn(part, text)

    def test_several_recipients(self):
        self.run_sender(self.success(), "--to", "other@example.com")
        self.assertEqual(FakePostmark.received[0][1]["To"], "me@example.com, other@example.com")

    def test_token_from_environment_without_env_file(self):
        r = self.run_sender(self.success(), env_file=False, token_env=TOKEN)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_variable(self):
        r = self.run_sender(self.success(), "--token-var", "NOPE")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOPE isn't set", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_missing_env_file(self):
        self.env_file = os.path.join(self.tmp, "gone.env")
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't read", r.stderr)

    def test_unreadable_output_file_sends_nothing(self):
        r = self.run_sender(self.success(expectedFile=os.path.join(self.tmp, "gone.md")))
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't read the job's output file", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_postmark_error(self):
        FakePostmark.reply = (422, {"ErrorCode": 300, "Message": "Invalid email request"}, {})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("Postmark error 300 (HTTP 422): Invalid email request", r.stderr)

    def test_error_that_echoes_the_token_is_redacted(self):
        FakePostmark.reply = (422, {"ErrorCode": 10, "Message": f"Bad token {TOKEN}"}, {})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("Bad token [token]", r.stderr)

    def test_redirect_is_not_followed(self):
        FakePostmark.reply = (302, {}, {"Location": "/elsewhere"})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(FakePostmark.received), 1)

    def test_only_http_200_with_error_code_0_succeeds(self):
        for reply in ((201, {"ErrorCode": 0}, {}), (200, [1, 2], {}), (200, "not json", {}),
                      (200, {"ErrorCode": "0"}, {})):
            FakePostmark.reply = reply
            self.assertEqual(self.run_sender(self.success()).returncode, 1, reply)

    def test_bad_payloads(self):
        for raw in ("not json", "[1]", json.dumps(self.success(version=2)), json.dumps(self.success(version=True))):
            r = self.run_sender(None, raw=raw)
            self.assertEqual(r.returncode, 1, raw)
            self.assertIn("payload", r.stderr, raw)
        self.assertEqual(FakePostmark.received, [])

    def test_token_with_a_newline_is_refused_without_leaking(self):
        r = self.run_sender(self.success(), env_file=False, token_env=TOKEN + "\nX-Other: 1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has spaces or control characters", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_truncated_error_reply_gives_one_line(self):
        FakePostmark.reply = (422, {"ErrorCode": 300, "Message": "x"}, {"Content-Length": "5000"})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith("notify_postmark: "), r.stderr)

    def test_api_url_override_must_be_local(self):
        r = self.run_sender(self.success(), api_url="https://elsewhere.example/email")
        self.assertEqual(r.returncode, 1)
        self.assertIn("may only point at a local test server", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_env_file_forms(self):
        p = os.path.join(self.tmp, "forms.env")
        with open(p, "w") as f:
            f.write("# c\n\nA=bare # note\nexport B='single # kept' # note\nC=\"double\"\nD=x\nD=y\nE=a#b\n")
        self.assertEqual(load_sender().read_env_file(p),
                         {"A": "bare", "B": "single # kept", "C": "double", "D": "y", "E": "a#b"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
