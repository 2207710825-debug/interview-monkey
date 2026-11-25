# -*- coding: utf-8 -*-
from pathlib import Path
import unittest

from 面试猴 import Api


ROOT = Path(__file__).resolve().parents[1]


class TestEmbeddedRealtime(unittest.TestCase):
    def test_main_renderer_embeds_realtime_only_in_ai_module(self):
        html = (ROOT / "renderer" / "index.html").read_text(encoding="utf-8")
        ai_start = html.index('id="mod-ai"')
        ai_end = html.index('<!-- ===== 模型设置 ===== -->', ai_start)
        ai_html = html[ai_start:ai_end]

        self.assertIn('id="aiRealtimeFrame"', ai_html)
        self.assertIn('id="aiRealtimeLoading"', ai_html)
        self.assertIn("get_realtime_url", html)
        self.assertIn("http://127.0.0.1:*", html)
        self.assertLess(html.index('id="mod-live"'), ai_start)

    def test_api_exposes_only_local_embedded_url(self):
        api = Api.__new__(Api)
        api._realtime_url = "http://127.0.0.1:12345/"
        self.assertEqual(
            api.get_realtime_url(),
            {"ok": True, "url": "http://127.0.0.1:12345/"},
        )

    def test_api_exposes_frameless_window_controls(self):
        for name in (
            "toggle_maximize_window", "close_window", "resize_window",
            "_apply_window_maximized_state",
        ):
            self.assertTrue(callable(getattr(Api, name, None)), name)
        source = (ROOT / "面试猴.py").read_text(encoding="utf-8")
        for marker in (
            "transparent=True",
            "frameless=True",
            "easy_drag=False",
            'DRAG_REGION_DIRECT_TARGET_ONLY"] = True',
            "events.maximized",
            "events.restored",
        ):
            self.assertIn(marker, source)
        html = (ROOT / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="theme-glass.css"', html)
        self.assertIn('id="resizeGrip"', html)
        self.assertTrue((ROOT / "renderer" / "theme-glass.css").is_file())

    def test_spec_keeps_original_desktop_entry_and_bundles_both_pages(self):
        spec = (ROOT / "面试猴.spec").read_text(encoding="utf-8")
        self.assertIn('project_dir / "面试猴.py"', spec)
        self.assertIn('project_dir / "renderer"', spec)
        self.assertIn('project_dir / "web"', spec)
        self.assertIn('project_dir / "engine" / "asr_models_zh_en"', spec)
        self.assertIn('project_dir / "engine" / "asr_models_sense"', spec)


if __name__ == "__main__":
    unittest.main()
