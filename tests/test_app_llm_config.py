# -*- coding: utf-8 -*-
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.realtime.api.app import LLMConfigManager, _app_llm_config
from engine.realtime.config import load_config
from engine.realtime.llm.providers import (
    LLMProviderError,
    OpenAICompatLLMProvider,
    UnavailableLLMProvider,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_app_config(root: Path, data: dict) -> Path:
    directory = root / "InterviewMonkey"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class FakeAnswerService:
    def __init__(self):
        self.llm_provider = object()
        self.transcript_corrector = object()
        self.hotwords = ()
        self.app_llm = {}

    def replace_llm(self, llm_provider, *, transcript_corrector=None, app_llm=None):
        self.llm_provider = llm_provider
        self.transcript_corrector = transcript_corrector
        if app_llm is not None:
            self.app_llm = dict(app_llm)


class TestAppLLMConfig(unittest.TestCase):
    def test_explicit_deepseek_key_wins_over_yaml_ollama(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_app_config(Path(directory), {
                "provider": "deepseek",
                "api_key": "sk-test-deepseek",
                "base_url": "",
                "model": "deepseek-chat",
                "ollama_model": "qwen2.5:3b",
            })
            with patch.dict(os.environ, {"APPDATA": directory}):
                config = _app_llm_config()

        self.assertIsNotNone(config)
        self.assertEqual(config["provider"], "deepseek")
        self.assertEqual(config["base_url"], "https://api.deepseek.com")
        self.assertEqual(config["model"], "deepseek-chat")
        self.assertEqual(config["api_key"], "sk-test-deepseek")
        self.assertTrue(config["configured"])
        self.assertTrue(config["cloud_text"])

    def test_missing_key_does_not_silently_fall_back_to_ollama(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_app_config(Path(directory), {
                "provider": "deepseek",
                "api_key": "",
                "model": "deepseek-chat",
            })
            with patch.dict(os.environ, {"APPDATA": directory}):
                config = _app_llm_config()

        self.assertIsNotNone(config)
        self.assertEqual(config["provider"], "deepseek")
        self.assertFalse(config["configured"])
        self.assertIn("API Key", config["error"])
        self.assertEqual(config["api_key"], "")

    def test_auto_with_key_resolves_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_app_config(Path(directory), {
                "provider": "auto",
                "api_key": "sk-test-deepseek",
                "model": "",
            })
            with patch.dict(os.environ, {"APPDATA": directory}):
                config = _app_llm_config()

        self.assertEqual(config["provider"], "deepseek")
        self.assertTrue(config["configured"])

    def test_missing_app_config_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"APPDATA": directory}):
                config = _app_llm_config()
        self.assertIsNone(config)


class TestLLMConfigManagerReload(unittest.TestCase):
    def test_reload_switches_provider_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_app_config(root, {
                "provider": "ollama",
                "api_key": "",
                "model": "",
                "ollama_model": "qwen2.5:3b",
            })
            service = FakeAnswerService()
            app = SimpleNamespace(state=SimpleNamespace(event_loop=None))
            cfg = load_config(ROOT / "config" / "config.yaml")
            with patch.dict(os.environ, {"APPDATA": directory}):
                manager = LLMConfigManager(app, service, cfg)
                unchanged = manager.reload()
                self.assertTrue(unchanged["ok"])
                self.assertFalse(unchanged["changed"])

                _write_app_config(root, {
                    "provider": "deepseek",
                    "api_key": "sk-test-deepseek",
                    "base_url": "",
                    "model": "deepseek-chat",
                    "ollama_model": "qwen2.5:3b",
                })
                result = manager.reload(force=True)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["changed"])
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertIsInstance(service.llm_provider, OpenAICompatLLMProvider)
        self.assertEqual(service.llm_provider.base_url, "https://api.deepseek.com")
        self.assertEqual(service.llm_provider.model, "deepseek-chat")
        self.assertEqual(service.llm_provider.api_key, "sk-test-deepseek")
        self.assertTrue(service.llm_provider.trust_env)
        self.assertIsNone(service.transcript_corrector)
        self.assertEqual(service.app_llm["provider"], "deepseek")
        self.assertTrue(service.app_llm["configured"])

    def test_missing_key_becomes_actionable_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_app_config(root, {"provider": "ollama", "ollama_model": "qwen2.5:3b"})
            service = FakeAnswerService()
            app = SimpleNamespace(state=SimpleNamespace(event_loop=None))
            cfg = load_config(ROOT / "config" / "config.yaml")
            with patch.dict(os.environ, {"APPDATA": directory}):
                manager = LLMConfigManager(app, service, cfg)
                _write_app_config(root, {
                    "provider": "deepseek",
                    "api_key": "",
                    "model": "deepseek-chat",
                })
                result = manager.reload(force=True)

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["configured"])
            self.assertIn("API Key", result["error"])
            self.assertIsInstance(service.llm_provider, UnavailableLLMProvider)

            async def read_error():
                try:
                    async for _ in service.llm_provider.stream([]):
                        pass
                except LLMProviderError as exc:
                    return exc
                return None

            error = asyncio.run(read_error())
            self.assertIsNotNone(error)
            self.assertEqual(error.category, "config")
            self.assertIn("API Key", str(error))


if __name__ == "__main__":
    unittest.main()
