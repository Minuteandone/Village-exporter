import json
import tempfile
import unittest
from pathlib import Path

from aivillage_export.exporter import VillageExporter


class FakeHttp:
    def __init__(self):
        self.default_delay = 1.25
        self.network_requests = 0
        self.cache_hits = 0
        self.responses = {}

    def set_host_delay(self, host, seconds):
        pass

    def get_json(self, url, **kwargs):
        self.network_requests += 1
        if url not in self.responses:
            raise AssertionError(f"unexpected URL: {url}")
        return self.responses[url]


class ExporterIntegrationTests(unittest.TestCase):
    def test_complete_day_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp()
            base = "https://theaidigest.org/village"
            http.responses[f"{base}/api/villages?slug=actual-launch-1"] = {"id": "v1", "slug": "actual-launch-1", "name": "Village"}
            http.responses[f"{base}/api/villages/v1"] = {
                "id": "v1", "slug": "actual-launch-1", "name": "Village", "villageGoal": "test",
                "agents": [{"id": "a1", "name": "Agent One"}],
                "chatRooms": [{"id": "r1", "name": "general"}],
                "chatMessages": [],
            }
            http.responses[f"{base}/api/villages/v1/active-dates"] = {"dates": ["2026-09-11"]}
            http.responses[f"{base}/api/events?villageId=v1&date=2026-09-11&page=1"] = {
                "events": [
                    {"id": "e1", "eventIndex": 1, "createdAt": "2026-09-11T16:00:00Z", "data": {"actionType": "AGENT_TALK", "speakerId": "a1", "messageId": "m1", "content": "hello", "roomId": "r1"}},
                    {"id": "e2", "eventIndex": 2, "createdAt": "2026-09-11T16:01:00Z", "data": {"actionType": "ENTER_ROOM", "agentId": "a1", "roomId": "old-room", "roomName": "temporary"}},
                    {"id": "e3", "eventIndex": 3, "createdAt": "2026-09-11T16:02:00Z", "data": {"actionType": "CONSOLIDATE", "agentId": "a1"}},
                    {"id": "e4", "eventIndex": 4, "createdAt": "2026-09-11T16:03:00Z", "data": {"actionType": "COMPUTER_ACTION", "agentId": "a1", "x": 1}},
                ],
                "hasMore": False,
            }
            http.responses[f"{base}/api/human-use-sessions?villageId=v1&date=2026-09-11"] = {"sessions": []}
            http.responses[f"{base}/api/agent/a1/memories?createdAt=1789182001000"] = {"memories": [{"id": "mem1", "agentId": "a1", "content": "memory", "createdAt": "2026-09-11T17:00:00Z", "updatedAt": "2026-09-11T17:00:00Z"}]}

            exporter = VillageExporter(http, Path(tmp), make_zip=True)
            village = exporter.load_village("actual-launch-1")
            result = exporter.export_day(village, "2026-09-11")
            day = Path(result["path"])
            self.assertTrue((day / "manifest.json").exists())
            self.assertTrue(Path(result["zip"]).exists())
            manifest = json.loads((day / "manifest.json").read_text())
            self.assertEqual(manifest["counts"]["rawEvents"], 4)
            activities = json.loads((day / "activities.json").read_text())
            self.assertEqual(len(activities), 3)
            self.assertIn("COMPUTER_ACTION", [a["actionType"] for a in activities])
            computer_steps = json.loads((day / "computer-steps.json").read_text())
            self.assertEqual(manifest["counts"]["computerSteps"], 1)
            self.assertEqual(computer_steps[0]["actionType"], "COMPUTER_ACTION")
            self.assertEqual(computer_steps[0]["rawEvent"]["id"], "e4")
            self.assertTrue((day / "computer-steps.jsonl").read_text().strip())
            rooms = json.loads((day / "rooms.discovered.json").read_text())
            self.assertIn("temporary", [r.get("name") for r in rooms])
            self.assertTrue((day / "checksums.sha256").read_text().strip())


if __name__ == "__main__":
    unittest.main()
