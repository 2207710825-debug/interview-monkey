# -*- coding: utf-8 -*-
import json
import struct
import unittest

from websockets.asyncio.server import serve

from engine.realtime.asr.providers import JsonWebSocketASRProvider
from engine.realtime.contracts import AudioFrame, Channel, TranscriptKind
from engine.realtime.mocks import MockAudioSource


class TestWebSocketAsr(unittest.IsolatedAsyncioTestCase):
    async def test_json_websocket_provider_final(self):
        received_types = []

        async def handler(websocket):
            async for message in websocket:
                data = json.loads(message)
                received_types.append(data["type"])
                if data["type"] == "stop":
                    await websocket.send(json.dumps({
                        "type": "final",
                        "text": "请解释RAG",
                        "channel": "loopback",
                        "start_ms": 0,
                        "end_ms": 500,
                        "confidence": 0.98,
                    }))
                    return

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            provider = JsonWebSocketASRProvider(
                f"ws://127.0.0.1:{port}", heartbeat_seconds=1, reconnect_attempts=2,
            )
            pcm = struct.pack("<" + "h" * 320, *([1200] * 320))
            source = MockAudioSource([AudioFrame(pcm, 0, Channel.LOOPBACK)])
            results = [item async for item in provider.transcribe(source.frames())]

        self.assertEqual([item.kind for item in results], [TranscriptKind.FINAL])
        self.assertEqual(results[0].text, "请解释RAG")
        self.assertEqual(received_types, ["start", "audio", "stop"])


if __name__ == "__main__":
    unittest.main()
