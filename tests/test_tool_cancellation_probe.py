from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError


ROOT = Path(__file__).parents[1].resolve()
PROBE_PATH = ROOT / "scripts/probe_tool_cancellation.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_tool_cancellation", PROBE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load probe module from {PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeHTTPResponse:
    def __init__(self, status: int = 204):
        self.status = status

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeProbeClient:
    instances = []

    def __init__(self, server: str, directory: Path):
        self.server = server
        self.directory = Path(directory)
        self.timeout = 1
        self.deleted = False
        self.abort_calls = []
        self.prompt_text = ""
        self.messages_calls = 0
        self.seen_git_repo = False
        type(self).instances.append(self)

    def _url(self, path: str, *, scoped: bool = False, query=None):
        self.last_url = f"{self.server}{path}"
        if scoped:
            self.last_url += f"?directory={self.directory}"
        return self.last_url

    def _headers(self):
        return {"Accept": "application/json"}

    def health(self):
        return {"healthy": True, "version": "fake-2.1.0"}

    def validate_model_selection(self, provider_id, model_id, effort):
        return {"providerID": provider_id, "modelID": model_id, "variant": effort}

    def create_session(self, title):
        self.seen_git_repo = (self.directory / ".git").is_dir()
        return {"id": "ses_probe"}

    def prompt_async(self, session_id, text, *, model, variant):
        self.prompt_text = text
        (self.directory / "quick-a").write_text("quick-a\n", encoding="utf-8")
        (self.directory / "quick-c").write_text("quick-c\n", encoding="utf-8")
        (self.directory / "sleeper.pid").write_text("99999999\n", encoding="utf-8")

    def messages(self, session_id, limit=100):
        self.messages_calls += 1
        target_status = "completed" if self.deleted else "running"
        messages = [
            {
                "info": {"id": "msg_target", "role": "assistant"},
                "parts": [
                    {
                        "id": "part_quick_a",
                        "messageID": "msg_target",
                        "callID": "call_quick_a",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "printf quick-a"}},
                    },
                    {
                        "id": "part_sleeper",
                        "messageID": "msg_target",
                        "callID": "call_sleeper",
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": target_status,
                            "input": {
                                "command": (
                                    "python3 -c 'import os,time; "
                                    "open(\"sleeper.pid\",\"w\").write(str(os.getpid())); "
                                    "time.sleep(120); open(\"sleeper-done\",\"w\").write(\"done\")'"
                                )
                            },
                        },
                    },
                    {
                        "id": "part_quick_c",
                        "messageID": "msg_target",
                        "callID": "call_quick_c",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "printf quick-c"}},
                    },
                ],
            },
            {
                "info": {"id": "msg_done", "role": "assistant"},
                "parts": [{"id": "part_done", "type": "text", "text": "finished"}],
            },
        ]
        return messages

    def abort(self, session_id):
        self.abort_calls.append(session_id)
        return True


class ProbeSafetyTest(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe()
        FakeProbeClient.instances = []

    def test_refuses_non_loopback_before_contacting_server(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            with self.assertRaisesRegex(ValueError, "non-loopback"):
                self.probe.run_probe(
                    "http://example.com:4096",
                    "mcli/glm-5.3",
                    "max",
                    output,
                )
            self.assertFalse(output.exists())

    def test_probe_uses_one_temporary_git_repo_and_aborts_in_finally(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            delete_requests = []

            def fake_urlopen(request, timeout):
                delete_requests.append(
                    {
                        "method": request.method,
                        "url": request.full_url,
                    }
                )
                self.assertEqual(request.method, "DELETE")
                self.assertIn(
                    "/session/ses_probe/message/msg_target/part/part_sleeper",
                    request.full_url,
                )
                self.assertNotIn("part_quick_a", request.full_url)
                self.assertNotIn("part_quick_c", request.full_url)
                FakeProbeClient.instances[0].deleted = True
                if len(delete_requests) == 2:
                    raise HTTPError(request.full_url, 404, "part already absent", {}, None)
                return FakeHTTPResponse()

            result = self.probe.run_probe(
                "http://127.0.0.1:4096",
                "mcli/glm-5.3",
                "max",
                output,
                client_factory=FakeProbeClient,
                poll_timeout=0.1,
                observe_timeout=0.1,
                poll_interval=0.001,
                sleeper=lambda _seconds: None,
                is_pid_alive=lambda _pid: False,
                production_root=ROOT,
                urlopen=fake_urlopen,
            )

            client = FakeProbeClient.instances[0]
            self.assertTrue(client.seen_git_repo)
            self.assertIn("quick-a", client.prompt_text)
            self.assertIn("quick-c", client.prompt_text)
            self.assertIn("sleep(120)", client.prompt_text)
            self.assertEqual(len(delete_requests), 2)
            self.assertEqual(client.abort_calls, ["ses_probe"])
            self.assertTrue(result["idempotent"])
            self.assertTrue(result["cleanup"]["temporary_repo_removed"])
            self.assertTrue(result["cleanup"]["session_abort_called"])
            self.assertEqual(result["target"], {
                "message_id": "msg_target",
                "part_id": "part_sleeper",
                "call_id": "call_sleeper",
            })
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(evidence["production_supported"])
            self.assertNotIn(str(ROOT), json.dumps(evidence))

    def test_abort_is_attempted_when_probe_fails_after_session_creation(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"

            class FailingClient(FakeProbeClient):
                def messages(self, session_id, limit=100):
                    raise RuntimeError("synthetic polling failure")

            result = self.probe.run_probe(
                "http://127.0.0.1:4096",
                "mcli/glm-5.3",
                "max",
                output,
                client_factory=FailingClient,
                poll_timeout=0.1,
                observe_timeout=0.1,
                poll_interval=0.001,
                sleeper=lambda _seconds: None,
                production_root=ROOT,
            )

            self.assertIn("failure", result)
            self.assertEqual(FailingClient.instances[0].abort_calls, ["ses_probe"])
            self.assertTrue(output.exists())

    def test_incomplete_evidence_does_not_expose_cancel_tool(self):
        from opencode_orchestrator.tools import TOOL_DEFINITIONS

        names = {item["name"] for item in TOOL_DEFINITIONS}
        self.assertNotIn("cancel_tool_call", names)

        evidence = {
            "tool_stopped": True,
            "model_resumed": True,
            "parallel_calls_valid": True,
            "transcript_consistent": True,
            "idempotent": False,
        }
        self.assertFalse(self.probe.production_supported(evidence))

    def test_delete_helper_uses_message_part_and_replay_is_harmless(self):
        client = FakeProbeClient("http://127.0.0.1:4096", Path("/tmp/probe"))
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request.method, request.full_url, timeout))
            return FakeHTTPResponse()

        first = self.probe.delete_tool_part(
            client,
            "ses_probe",
            "msg_target",
            "part_sleeper",
            urlopen=fake_urlopen,
        )
        second = self.probe.delete_tool_part(
            client,
            "ses_probe",
            "msg_target",
            "part_sleeper",
            urlopen=fake_urlopen,
        )

        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(method == "DELETE" for method, _url, _timeout in requests))
        self.assertTrue(all("/message/msg_target/part/part_sleeper" in url for _method, url, _timeout in requests))


if __name__ == "__main__":
    unittest.main()
