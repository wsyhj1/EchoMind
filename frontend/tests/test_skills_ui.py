"""Public metadata, native chat input preservation, and pure palette logic."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
from skills_ui import PUBLIC_FIELDS, public_catalog

CATALOG = [
    {"id": "summarize", "name": "摘要", "description": "总结用户提供的内容", "aliases": ["总结", "summary"],
     "kind": "agent", "requires_input": True, "default_input": "", "examples": ["/summarize 一段文字"]},
    {"id": "explain", "name": "解释", "description": "解释问题", "aliases": ["说明"],
     "kind": "agent", "requires_input": True, "default_input": "", "examples": ["&explain 一个概念"]},
    {"id": "help", "name": "帮助", "description": "查看技能与命令", "aliases": ["帮助"],
     "kind": "local", "requires_input": False, "default_input": "", "examples": ["/help"]},
]


class SkillsUITests(unittest.TestCase):
    def palette_app(self, disabled=False):
        return AppTest.from_string('import streamlit as st\nfrom skills_ui import render_skill_palette\n'
                                  'st.chat_input("保留原生输入框")\n'
                                  f'render_skill_palette({CATALOG!r}, disabled={disabled!r})', default_timeout=15)

    def test_only_public_metadata_is_serialized_and_invalid_ids_are_rejected(self):
        private = {**CATALOG[0], "instructions": "PRIVATE SYSTEM INSTRUCTIONS", "handler": lambda: None}
        result = public_catalog([private, CATALOG[0], {"id": "help\nsubmit"}, {"id": "../secret"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]), set(PUBLIC_FIELDS))
        self.assertNotIn("PRIVATE SYSTEM", json.dumps(result))
        self.assertEqual(result[0]["examples"], ["/summarize 一段文字"])
        self.assertEqual(public_catalog(None), [])

    def test_zero_height_mount_keeps_the_native_chat_input_and_disabled_flag(self):
        renderer = MagicMock()
        with patch("skills_ui._get_renderer", return_value=renderer):
            app = self.palette_app(disabled=True).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.chat_input), 1)
        self.assertEqual(renderer.call_args.kwargs["height"], 0)
        self.assertTrue(renderer.call_args.kwargs["data"]["disabled"])
        self.assertEqual(renderer.call_args.kwargs["data"]["catalog"], public_catalog(CATALOG))

    def test_real_component_registration_survives_two_independent_app_runtimes(self):
        # No production bypass and no mocked component registration: the second
        # manager must not receive a cached renderer absent from its registry.
        first = self.palette_app().run()
        self.assertFalse(first.exception)
        second = self.palette_app().run()
        self.assertFalse(second.exception)
        first.run()
        self.assertFalse(first.exception)
        self.assertEqual(len(first.chat_input), 1)
        self.assertEqual(len(second.chat_input), 1)

    def test_local_help_searches_chinese_aliases_and_exposes_no_instruction_body(self):
        catalog = [*CATALOG, {"id": "private_example", "name": "示例", "instructions": "PRIVATE BODY"}]
        app = AppTest.from_string('from skills_ui import render_skill_help\n'
                                  f'render_skill_help({catalog!r})', default_timeout=15)
        app.session_state.skills_help_open = True
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("PRIVATE BODY", str(app.dataframe[0].value))
        app.text_input(key="skill_help_query").set_value("说明").run()
        self.assertFalse(app.exception)
        frame = app.dataframe[0].value
        self.assertEqual(list(frame["名称"]), ["解释"])
        self.assertEqual(list(frame["命令"]), ["/explain · &explain"])
        app.button(key="skills_help_close").click().run()
        self.assertFalse(app.session_state.skills_help_open)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; browser acceptance checks DOM interaction separately")
    def test_javascript_prefix_search_and_command_fill_use_actual_module_functions(self):
        source = (FRONTEND / "skill_palette.js").read_bytes()
        module = "data:text/javascript;base64," + base64.b64encode(source).decode("ascii")
        script = ('const m=await import(' + json.dumps(module) + ');\nconst catalog=' + json.dumps(CATALOG, ensure_ascii=False) + ';\n'
                  'process.stdout.write(JSON.stringify({slash:m.prefixQuery("/"),amp:m.prefixQuery("&总结"),'
                  'middle:m.prefixQuery("文字 /summarize"),space:m.prefixQuery(" /summarize"),'
                  'body:m.prefixQuery("/summarize 正文"),'
                  'escapedSlash:m.prefixQuery("//summary"),escapedAmp:m.prefixQuery("&&摘要"),'
                  'chinese:m.filterCatalog(catalog,"总结").map(s=>s.id),'
                  'latin:m.filterCatalog(catalog,"SUMM").map(s=>s.id),'
                  'fill:m.commandValue("&sum 原稿","explain"),'
                  'draft:m.commandValue("保留这段稿件","summarize"),'
                  'empty:m.commandValue("/su","summarize"),'
                  'invalid:m.commandValue("/","help\\nsubmit")}));')
        process = subprocess.run([shutil.which("node"), "--input-type=module"], input=script, text=True,
                                 encoding="utf-8", capture_output=True, timeout=15, check=True)
        result = json.loads(process.stdout)
        self.assertEqual(result["slash"], {"prefix": "/", "query": ""})
        self.assertEqual(result["amp"], {"prefix": "&", "query": "总结"})
        self.assertIsNone(result["middle"])
        self.assertIsNone(result["space"])
        self.assertIsNone(result["body"])
        self.assertIsNone(result["escapedSlash"])
        self.assertIsNone(result["escapedAmp"])
        self.assertEqual(result["chinese"], ["summarize"])
        self.assertEqual(result["latin"], ["summarize"])
        self.assertEqual(result["fill"], "&explain 原稿")
        self.assertEqual(result["draft"], "/summarize 保留这段稿件")
        self.assertEqual(result["empty"], "/summarize ")
        self.assertIsNone(result["invalid"])


if __name__ == "__main__":
    unittest.main()
