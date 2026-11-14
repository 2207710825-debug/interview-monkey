# -*- coding: utf-8 -*-
import os
import unittest

import httpx

from engine.realtime.api.app import create_app
from engine.realtime.contracts import Chunk
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.mocks import MockLLMProvider, MockRetriever


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def make_service(pieces=None):
    chunks = [Chunk("kafka-1", "Kafka Broker 持久化消息。", "kb.md", 0.9, "Kafka")]
    return RealtimeAnswerService(
        MockRetriever(chunks),
        MockLLMProvider(pieces or ["**结论**：", "使用 **Kafka Broker**。", "\n**关键词**：**Kafka**"]),
        os.path.join(ROOT, "prompts", "answer_system.md"),
    )


class TestRealtimeSse(unittest.IsolatedAsyncioTestCase):
    async def test_service_event_order_and_trace(self):
        events = [event async for event in make_service().stream_answer("Kafka如何持久化", session_id="s1")]
        self.assertEqual([event.event for event in events], ["meta", "token", "token", "token", "done"])
        self.assertEqual(len({event.trace_id for event in events}), 1)
        self.assertEqual(events[0].data["sources"][0]["chunk_id"], "kafka-1")
        self.assertIsNotNone(events[-1].data["metrics"]["ttft_ms"])

    async def test_fastapi_sse_contract(self):
        transport = httpx.ASGITransport(app=create_app(make_service()))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/answer/stream", json={
                "question": "Kafka如何持久化",
                "session_id": "s2",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        event_names = [line[7:] for line in response.text.splitlines() if line.startswith("event: ")]
        self.assertEqual(event_names[0], "meta")
        self.assertIn("token", event_names)
        self.assertEqual(event_names[-1], "done")

    async def test_low_confidence_meta(self):
        service = RealtimeAnswerService(
            MockRetriever([]), MockLLMProvider(["知识库中未找到明确依据"]),
            os.path.join(ROOT, "prompts", "answer_system.md"),
        )
        events = [event async for event in service.stream_answer("未知问题")]
        self.assertTrue(events[0].data["low_confidence"])

    async def test_knowledge_list_and_switch(self):
        transport = httpx.ASGITransport(app=create_app(make_service()))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listing = (await client.get("/api/knowledge")).json()
            self.assertTrue(listing["items"])
            categories = [item for item in listing["items"] if item["path"].startswith("category:")]
            self.assertTrue(categories)
            switched = await client.post("/api/knowledge/switch", json={"path": listing["items"][0]["path"]})
            category_switch = await client.post(
                "/api/knowledge/switch", json={"path": categories[0]["path"]},
            )
        self.assertEqual(switched.status_code, 200)
        self.assertTrue(switched.json()["ok"])
        self.assertGreater(switched.json()["chunks"], 0)
        self.assertEqual(category_switch.status_code, 200)
        self.assertEqual(category_switch.json()["category"], categories[0]["category"])
        self.assertGreater(category_switch.json()["chunks"], 0)


if __name__ == "__main__":
    unittest.main()
