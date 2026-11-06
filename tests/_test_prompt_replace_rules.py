"""Prompt modes as five parallel choices, incl. exact and paragraph replace.

The panel picks exactly one mode (`prompt.mode`) and each mode reads only its
own input: append/custom use the gateway prompt body (`prompt.text` /
`prompt.file`), replace_exact/replace_paragraph use `prompt.replace_rules` and
rewrite the client's system/developer text in place without injecting anything.
Nothing stacks on top of another mode any more. user/assistant/tool messages
are never touched. Run with: python _test_prompt_replace_rules.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="prompt-replace-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_prompt
import wb_proxy
import wb_settings


class RuleTextTests(unittest.TestCase):
    def test_parse_and_render_round_trip(self):
        rules = wb_prompt.parse_rules_text("one => two\n空 =>\n")
        self.assertEqual(rules, [{"old": "one", "new": "two"},
                                  {"old": "空", "new": ""}])
        self.assertEqual(wb_prompt.rules_to_text(rules),
                         "one => two\n空 => ")
        # 无 => 的行、空行忽略；按首个 => 切分。
        self.assertEqual(wb_prompt.parse_rules_text("no arrow\na => b => c"),
                          [{"old": "a", "new": "b => c"}])

    def test_replace_text_sequential_and_identity(self):
        rules = [{"old": "one", "new": "two"}, {"old": "two", "new": "three"}]
        self.assertEqual(wb_prompt.replace_text("one", rules), "three")
        text = "nothing here"
        self.assertIs(wb_prompt.replace_text(text, [{"old": "zz", "new": "y"}]),
                      text)
        self.assertEqual(wb_prompt.replace_text("abc", None), "abc")

    def test_apply_replace_content_parts(self):
        parts: list = [{"type": "text", "text": "one"}, {"type": "image_url"}]
        out = wb_prompt.apply_replace_content(parts, [{"old": "one", "new": "2"}])
        self.assertEqual(out[0]["text"], "2")
        self.assertEqual(parts[0]["text"], "one")  # 原件不被就地修改
        same = [{"type": "text", "text": "one"}]
        self.assertIs(wb_prompt.apply_replace_content(same, []), same)
        self.assertIs(wb_prompt.apply_replace_content(same,
                      [{"old": "miss", "new": "x"}]), same)


class ParagraphTextTests(unittest.TestCase):
    """整段替换：段落以空行分隔，命中即整段换掉。"""

    def test_hit_replaces_the_whole_paragraph(self):
        text = "keep me.\n\nTokens prefixed with @ are paths. Blah blah more.\n\n尾段。"
        out = wb_prompt.replace_paragraph_text(text, [
            {"old": "Tokens prefixed with @", "new": "以 @ 开头的标记是路径。"}])
        self.assertEqual(out, "keep me.\n\n以 @ 开头的标记是路径。\n\n尾段。")

    def test_empty_new_deletes_paragraph_and_one_separator(self):
        text = "A line.\n\nB line.\n\nC line."
        out = wb_prompt.replace_paragraph_text(text, [{"old": "B", "new": ""}])
        self.assertEqual(out, "A line.\n\nC line.")

    def test_first_paragraph_delete_does_not_lead_with_separator(self):
        text = "A line.\n\nB line."
        out = wb_prompt.replace_paragraph_text(text, [{"old": "A", "new": ""}])
        self.assertEqual(out, "B line.")

    def test_last_paragraph_delete_keeps_text_clean(self):
        text = "A line.\n\nB line."
        out = wb_prompt.replace_paragraph_text(text, [{"old": "B", "new": ""}])
        self.assertEqual(out, "A line.")

    def test_multiple_matching_paragraphs_all_get_replaced(self):
        # 与精确替换口径一致：每个命中段落都换，出现两处就都换。
        text = "has A here\n\nuntouched\n\nhas A too"
        out = wb_prompt.replace_paragraph_text(text, [{"old": "A", "new": "X"}])
        self.assertEqual(out, "X\n\nuntouched\n\nX")

    def test_no_cascade_one_paragraph_matches_first_rule_only(self):
        # 第一段命中规则1后变成 "two"，规则2 不该再把它改成 "three"。
        text = "one\n\nuntouched"
        out = wb_prompt.replace_paragraph_text(text, [
            {"old": "one", "new": "two"}, {"old": "two", "new": "three"}])
        self.assertEqual(out, "two\n\nuntouched")
        # 精确替换则是叠加的，两者语义不同。
        self.assertEqual(wb_prompt.replace_text(text, [
            {"old": "one", "new": "two"}, {"old": "two", "new": "three"}]),
            "three\n\nuntouched")

    def test_separators_are_preserved_verbatim(self):
        text = "A\n  \nB\n\nC"       # 第一段之间是空白行分隔，原文要留着
        out = wb_prompt.replace_paragraph_text(text, [{"old": "B", "new": "X"}])
        self.assertEqual(out, "A\n  \nX\n\nC")

    def test_miss_returns_same_object(self):
        text = "nothing to match"
        self.assertIs(wb_prompt.replace_paragraph_text(
            text, [{"old": "zz", "new": "y"}]), text)


class ApplyMessageTests(unittest.TestCase):
    def test_only_system_and_developer_are_rewritten(self):
        msgs = [{"role": "system", "content": "You are one."},
                {"role": "developer", "content": [{"type": "text", "text": "one"}]},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "one"}]
        rules = [{"old": "one", "new": "two"}]
        out = wb_prompt.apply_replacements(msgs, rules)
        self.assertEqual(out[0]["content"], "You are two.")
        self.assertEqual(out[1]["content"][0]["text"], "two")
        self.assertEqual(out[2]["content"], "one")
        self.assertEqual(out[3]["content"], "one")

    def test_paragraph_mode_also_spares_other_roles(self):
        para = wb_prompt.apply_paragraph_replacements(
            [{"role": "system", "content": "para one\n\nkeep"},
             {"role": "user", "content": "para one"}],
            [{"old": "one", "new": "two"}])
        self.assertEqual(para[0]["content"], "two\n\nkeep")
        self.assertEqual(para[1]["content"], "para one")

    def test_no_hit_returns_same_object(self):
        msgs = [{"role": "system", "content": "calm"}]
        self.assertIs(wb_prompt.apply_replacements(
            msgs, [{"old": "zz", "new": "y"}]), msgs)
        self.assertIs(wb_prompt.apply_paragraph_replacements(
            msgs, [{"old": "zz", "new": "y"}]), msgs)


class ApplyModeTests(unittest.TestCase):
    """每个模式各用自己的输入，互不叠加。"""

    def test_passthrough_ignores_rules_entirely(self):
        msgs = [{"role": "system", "content": "A one B"}]
        self.assertIs(wb_prompt.apply_mode(
            msgs, "passthrough", "", replace_rules=[{"old": "one", "new": "two"}]),
            msgs)

    def test_custom_does_not_apply_rules_to_the_gateway_body(self):
        out = wb_prompt.apply_mode(
            [{"role": "system", "content": "client one"},
             {"role": "user", "content": "hi"}],
            "custom", "You are one.",
            replace_rules=[{"old": "one", "new": "two"}])
        self.assertEqual(out[0], {"role": "system", "content": "You are one."})
        self.assertEqual(out[1]["content"], "hi")

    def test_append_does_not_apply_rules_to_client_or_gateway(self):
        out = wb_prompt.apply_mode(
            [{"role": "system", "content": "client one"},
             {"role": "user", "content": "hi"}],
            "append", "gw one",
            replace_rules=[{"old": "one", "new": "two"}])
        self.assertEqual(out[0]["content"], "client one")   # 客户端逐字保留
        self.assertEqual(out[1], {"role": "system", "content": "gw one"})
        self.assertEqual(out[2]["content"], "hi")

    def test_replace_exact_keeps_every_message(self):
        msgs = [{"role": "system", "content": "You are one."},
                {"role": "user", "content": "say one"}]
        out = wb_prompt.apply_mode(
            msgs, "replace_exact", "IGNORED",
            replace_rules=[{"old": "one", "new": "two"}])
        self.assertEqual(len(out), 2)                   # 没有注入任何正文
        self.assertEqual(out[0]["content"], "You are two.")
        self.assertEqual(out[1]["content"], "say one")  # 正文也没被塞进来

    def test_replace_paragraph_keeps_every_message(self):
        msgs = [{"role": "system", "content": "para A\n\npara B"},
                {"role": "user", "content": "hi"}]
        out = wb_prompt.apply_mode(
            msgs, "replace_paragraph", "IGNORED",
            replace_rules=[{"old": "para A", "new": "段落甲"}])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "段落甲\n\npara B")
        self.assertEqual(out[1]["content"], "hi")

    def test_replace_modes_never_enter_the_degrade_path(self):
        for mode in wb_prompt.REPLACE_MODES:
            msgs = [{"role": "system", "content": "one"},
                    {"role": "user", "content": "hi"}]
            out = wb_prompt.apply_mode(msgs, mode, "", degraded=True,
                                       replace_rules=[{"old": "one", "new": "two"}])
            self.assertEqual(out[0]["content"], "two", mode)

    def test_passthrough_and_append_still_degrade(self):
        for mode in ("passthrough", "append"):
            out = wb_prompt.apply_mode([{"role": "user", "content": "hi"}],
                                       mode, "gw", degraded=True)
            self.assertEqual(out[0]["content"], wb_prompt.DEGRADED_PROMPT, mode)

    def test_unknown_mode_falls_back_to_passthrough(self):
        msgs = [{"role": "system", "content": "one"}]
        self.assertIs(wb_prompt.apply_mode(
            msgs, "replace_all", "",
            replace_rules=[{"old": "one", "new": "two"}]), msgs)


class SettingsRoundTripTests(unittest.TestCase):
    def test_defaults_include_empty_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(wb_settings.prompt_config(directory),
                             {"mode": "passthrough", "file": "", "text": "",
                              "replace_rules": []})

    def test_every_mode_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in wb_prompt.VALID_MODES:
                wb_settings.set_prompt_config(directory, {"mode": mode})
                self.assertEqual(wb_settings.prompt_config(directory)["mode"], mode)

    def test_rules_round_trip_and_partial_update(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_prompt_config(directory, {
                "mode": "replace_exact",
                "replace_rules": [{"old": "one", "new": "two"}]})
            cfg = wb_settings.prompt_config(directory)
            self.assertEqual(cfg["replace_rules"], [{"old": "one", "new": "two"}])
            # 只改 mode 不该把规则冲掉。
            wb_settings.set_prompt_config(directory, {"mode": "replace_paragraph"})
            self.assertEqual(
                wb_settings.prompt_config(directory)["replace_rules"],
                [{"old": "one", "new": "two"}])

    def test_validation_rejects_bad_mode_and_rules(self):
        for bad in ({"mode": "replace_all"}, {"mode": 1},
                    {"replace_rules": "x"},
                    {"replace_rules": ["x"]},
                    {"replace_rules": [{"old": "", "new": "t"}]},
                    {"replace_rules": [{"old": 1, "new": "t"}]},
                    {"replace_rules": [{"old": "a", "new": None}]},
                    {"replace_rules": [{"old": "a", "new": "b"}]
                     * (wb_settings.PROMPT_REPLACE_RULES_MAX + 1)},
                    {"replace_rules": [{"old": "x" * (wb_settings.PROMPT_REPLACE_FIELD_MAX + 1),
                                        "new": "y"}]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb_settings.validate_prompt_patch(bad)

    def test_hand_edited_file_is_read_leniently(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(wb_settings.settings_path(directory), "w",
                      encoding="utf-8") as fh:
                json.dump({"prompt": {"mode": "replace_paragraph", "replace_rules": [
                    {"old": "ok", "new": "fine"}, "junk",
                    {"old": "", "new": "x"}, {"from": "legacy", "to": "mapped"},
                    {"old": 7, "new": "y"}]}}, fh)
            cfg = wb_settings.prompt_config(directory)
            self.assertEqual(cfg["mode"], "replace_paragraph")
            self.assertEqual(cfg["replace_rules"],
                             [{"old": "ok", "new": "fine"},
                              {"old": "legacy", "new": "mapped"}])


class BuildBodyModeTests(unittest.TestCase):
    def setUp(self):
        wb_proxy.PROMPT_DEGRADE.reset()

    def tearDown(self):
        wb_proxy.PROMPT_DEGRADE.reset()

    def build(self, directory, messages, **extra):
        payload = {"model": "deepseek-v4.1-flash", "messages": messages}
        payload.update(extra)
        with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
            return wb_proxy.build_upstream_body(payload)

    def test_replace_exact_reaches_the_upstream_body(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_prompt_config(directory, {
                "mode": "replace_exact",
                "replace_rules": [{"old": "one", "new": "two"}]})
            body = self.build(directory, [
                {"role": "system", "content": "You are one."},
                {"role": "user", "content": "say one"}])
        self.assertEqual(body["messages"][0]["content"], "You are two.")
        self.assertEqual(body["messages"][1]["content"], "say one")

    def test_replace_paragraph_reaches_the_upstream_body(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_prompt_config(directory, {
                "mode": "replace_paragraph",
                "replace_rules": [{"old": "Tokens prefixed with @",
                                   "new": "以 @ 开头的是路径。"}]})
            body = self.build(directory, [
                {"role": "system",
                 "content": ("Intro line.\n\n"
                             "Tokens prefixed with @ are paths the user "
                             "referenced. Lots of other words here.\n\nTail.")},
                {"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][0]["content"],
                         "Intro line.\n\n以 @ 开头的是路径。\n\nTail.")

    def test_replace_mode_without_rules_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_prompt_config(directory, {"mode": "replace_exact"})
            body = self.build(directory, [
                {"role": "system", "content": "one"},
                {"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][0]["content"], "one")

    def test_custom_keeps_its_own_body_unconditional(self):
        with tempfile.TemporaryDirectory() as directory:
            wb_settings.set_prompt_config(directory, {
                "mode": "custom", "text": "GATEWAY one",
                "replace_rules": [{"old": "one", "new": "two"}]})
            body = self.build(directory, [
                {"role": "system", "content": "client one"},
                {"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][0]["content"], "GATEWAY one")

    def test_empty_rules_keep_identity_passthrough(self):
        with tempfile.TemporaryDirectory() as directory:
            body = self.build(directory, [
                {"role": "system", "content": "CLIENT-SYS"},
                {"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][0]["content"], "CLIENT-SYS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
