import unittest

from aivillage_export.normalize import action_category, build_timeline, map_activities, map_messages


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.agents = [{"id": "a1", "name": "Agent One"}]
        self.events = [
            {"id": "1", "eventIndex": 1, "createdAt": "2026-09-11T16:00:00Z", "data": {"actionType": "AGENT_TALK", "speakerId": "a1", "messageId": "m1", "content": "hello", "roomId": "r1"}},
            {"id": "2", "eventIndex": 2, "createdAt": "2026-09-11T16:01:00Z", "data": {"actionType": "PAUSE", "agentId": "a1", "seconds": 60}},
            {"id": "3", "eventIndex": 3, "createdAt": "2026-09-11T16:02:00Z", "data": {"actionType": "COMPUTER_USE", "agentId": "a1", "url": "https://example.invalid"}},
        ]

    def test_messages(self):
        messages = map_messages(self.events, self.agents)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["speakerName"], "Agent One")

    def test_all_non_chat_activity_is_kept(self):
        activities = map_activities(self.events, self.agents)
        self.assertEqual([a["actionType"] for a in activities], ["PAUSE", "COMPUTER_USE"])
        self.assertEqual(activities[1]["category"], "computer")

    def test_timeline_adds_helper_turns(self):
        sessions = [{"id": "s1", "agentId": "a1", "createdAt": "2026-09-11T16:03:00Z", "userIntro": "I can help", "turns": [{"id": "t1", "createdAt": "2026-09-11T16:04:00Z", "updatedAt": "2026-09-11T16:05:00Z", "agentAction": {"instructions": "click it"}, "userResponse": "done"}]}]
        timeline = build_timeline(self.events, self.agents, sessions)
        self.assertEqual(len(timeline), 6)
        self.assertEqual(sum(1 for item in timeline if item["kind"] == "human-helper-context"), 3)

    def test_categories(self):
        self.assertEqual(action_category("CONSOLIDATE"), "consolidation")
        self.assertEqual(action_category("BROWSER_ACTION"), "computer")
        self.assertEqual(action_category("OUTREACH_APPROVAL_REQUEST"), "outreach")


if __name__ == "__main__":
    unittest.main()
