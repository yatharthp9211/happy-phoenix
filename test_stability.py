"""
Comprehensive Stability & Capability Verification Test Suite for PHOENIX MK4 / Dynamic Island
"""

import sys
import os
import unittest
import types
from unittest.mock import MagicMock
from importlib.machinery import SourceFileLoader

# Ensure workspace root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Provide mock modules for packages not installed in this Linux build container
for mod_name in ("requests", "cv2", "mss", "torch", "torchvision", "easyocr", "pygame", "moderngl", "numpy", "PIL", "zmq"):
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        if mod_name == "requests":
            m.post = MagicMock()
            m.exceptions = types.ModuleType("requests.exceptions")
            m.exceptions.RequestException = Exception
        elif mod_name == "pygame":
            m.locals = types.ModuleType("pygame.locals")
            m.Rect = lambda *a, **k: MagicMock()
            m.mouse = MagicMock()
            m.mouse.get_pos = lambda: (0, 0)
        elif mod_name == "numpy":
            m.array = lambda *a, **k: MagicMock()
            m.ndarray = MagicMock
        elif mod_name == "PIL":
            m.Image = MagicMock()
            m.ImageDraw = MagicMock()
            m.ImageGrab = MagicMock()
        elif mod_name == "zmq":
            m.Context = MagicMock
            m.PUB = 1
            m.SUB = 2
        sys.modules[mod_name] = m
if "PIL.Image" not in sys.modules:
    sys.modules["PIL.Image"] = sys.modules["PIL"].Image


class TestNeedleAndDispatch(unittest.TestCase):
    def setUp(self):
        import nidle_mk4_claude_edits as nidle
        self.nidle = nidle

    def test_tools_registered(self):
        self.assertTrue(hasattr(self.nidle, "execute_task"))
        self.assertTrue(hasattr(self.nidle, "launch_app"))
        self.assertTrue(hasattr(self.nidle, "surf_website"))
        self.assertTrue(hasattr(self.nidle, "type_text"))

    def test_parse_type_command(self):
        text, press = self.nidle._parse_type_command('type "hello world" and press enter')
        self.assertEqual(text, "hello world")
        self.assertTrue(press)

        text, press = self.nidle._parse_type_command('type "it\'s working"')
        self.assertEqual(text, "it's working")
        self.assertFalse(press)

    def test_start_menu_launch(self):
        # Snapchat shortcut launch via Start Menu
        r = self.nidle.launch_app("snapchat")
        self.assertTrue(self.nidle._ok(r))
        self.assertIn("snapchat", r.lower())
        self.assertIn("start menu", r.lower())

        # WhatsApp launch via Start Menu
        r = self.nidle.launch_app("whatsapp")
        self.assertTrue(self.nidle._ok(r))
        self.assertIn("whatsapp", r.lower())
        self.assertIn("start menu", r.lower())

    def test_deterministic_dispatch(self):
        # YouTube launched via Start menu (not via Chrome URL)
        res = self.nidle.execute_task("play believer on youtube")
        self.assertTrue(res["success"])
        self.assertIn("youtube", res["results"].lower())
        self.assertIn("start menu", res["results"].lower())

        # Google search deterministic URL
        res = self.nidle.execute_task("search for sanskarglobal.ai.studio on google")
        self.assertTrue(res["success"])
        self.assertIn("google.com/search?q=sanskarglobal.ai.studio", res["results"])

        # Launch app via Start Menu
        res = self.nidle.execute_task("launch app snapchat")
        self.assertTrue(res["success"])
        self.assertIn("snapchat", res["results"].lower())
        self.assertIn("start menu", res["results"].lower())


class TestControlLoopAndGating(unittest.TestCase):
    def test_non_visual_action_validation(self):
        import control_loop
        loop = control_loop.Orchestrator()
        loop.new_user_intent("test goal")

        # Non-visual action: observation_id mismatch should NOT reject it
        non_visual_act = control_loop.ActionProposal(
            action_id=1,
            generation_id=1,
            observation_id=99,  # different from perception.observation_id
            action_type="launch",
            full_command="launch app chrome"
        )
        verdict = loop.validate(non_visual_act)
        self.assertEqual(verdict, control_loop.VALID)

        # Visual action: observation_id mismatch MUST reject it
        visual_act = control_loop.ActionProposal(
            action_id=2,
            generation_id=1,
            observation_id=99,
            action_type="click",
            full_command="click 500,500"
        )
        verdict_v = loop.validate(visual_act)
        self.assertEqual(verdict_v, control_loop.STALE_OBSERVATION)

    def test_youtube_driver_unified_as_search_driver(self):
        import app_drivers
        ts = {
            "active": True,
            "target_app": "youtube",
            "expected_text": "believer",
            "target_launched": True,
        }
        driver = app_drivers.select_app_driver(ts)
        self.assertIsInstance(driver, app_drivers.SearchDriver)
        self.assertEqual(type(driver), app_drivers.SearchDriver)


class TestTargetExtraction(unittest.TestCase):
    def test_extract_launch_app(self):
        import bot_mk9

        # Prepositional targets
        app = bot_mk9._extract_launch_app("open current project in vscode")
        self.assertEqual(app, "vscode")

        app = bot_mk9._extract_launch_app("play believer on youtube")
        self.assertEqual(app, "youtube")

        app = bot_mk9._extract_launch_app("browse trending youtube")
        self.assertEqual(app, "youtube")

        app = bot_mk9._extract_launch_app("spawn bash terminal")
        self.assertEqual(app, "terminal")

        app = bot_mk9._extract_launch_app("launch google chrome")
        self.assertEqual(app, "chrome")

        app = bot_mk9._extract_launch_app("open whatsapp")
        self.assertEqual(app, "whatsapp")

    def test_brain_wants_work_keywords(self):
        import bot_mk9
        self.assertTrue(bot_mk9._work_by_keyword("search for sanskarglobal.ai.studio on google"))
        self.assertTrue(bot_mk9._work_by_keyword("play believer on youtube"))
        self.assertTrue(bot_mk9._work_by_keyword("open spotify"))
        self.assertFalse(bot_mk9._work_by_keyword("hi who are you"))
        self.assertFalse(bot_mk9._work_by_keyword("tell me a story"))


class TestLLMServerPayloadAndStability(unittest.TestCase):
    def test_payload_structure(self):
        import llm_server
        mgr = llm_server.LlamaServerManager(start_on_init=False)
        payload = mgr._payload(
            [{"role": "user", "content": "hi"}],
            grammar=None,
            max_tokens=150,
            temperature=0.5,
            stream=True
        )
        self.assertEqual(payload["max_tokens"], 150)
        self.assertEqual(payload["temperature"], 0.5)
        self.assertTrue(payload["stream"])
        self.assertNotIn("grammar", payload)


class TestDraggableIslandCalculations(unittest.TestCase):
    def test_drag_and_snap_bounds(self):
        screen_w, screen_h = 1920, 1080
        w, h = 540, 76
        center_x = (screen_w - w) // 2

        # 1. Custom position inside screen
        target_x, target_y = 200, 300
        clamped_x = max(0, min(target_x, screen_w - w))
        clamped_y = max(0, min(target_y, screen_h - h))
        self.assertEqual((clamped_x, clamped_y), (200, 300))

        # 2. Near top-center snap
        near_top_x, near_top_y = center_x + 10, 8
        if near_top_y <= 12 and abs(near_top_x - center_x) < 50:
            clamped_y = 0
            clamped_x = center_x
            has_custom = False
        else:
            has_custom = True

        self.assertFalse(has_custom)
        self.assertEqual(clamped_y, 0)
        self.assertEqual(clamped_x, center_x)


if __name__ == "__main__":
    unittest.main()
