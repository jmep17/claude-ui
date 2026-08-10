"""The local-model (oMLX) setup piece: config validation, the generated
claude-local.sh wrapper, the zshrc block, pricing overrides, and the probe.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_localmodel.py`.

The Injection cases are the security battery: config values are spliced into
a shell script inside single quotes, so anything that could escape a single
quote must be refused at save time, never quoted around."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, localmodel, projects, settings  # noqa: E402


class Base(unittest.TestCase):
    """Tempdir config dir, .claude-ui.json, and ZDOTDIR, patched into core."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.cfg = base / "config"
        self.cfg.mkdir()
        self.home = base / "home"
        self.home.mkdir()
        self._config_dir = core.config_dir
        core.config_dir = lambda: self.cfg
        self._lm_config_dir = localmodel.config_dir
        localmodel.config_dir = core.config_dir
        # localmodel writes the picker env pair through settings_set_many,
        # which resolves the settings.json path via its own binding — patched
        # so apply can never touch the developer's real ~/.claude
        self._s_config_dir = settings.config_dir
        settings.config_dir = core.config_dir
        self._cfg_file = core.CONFIG_FILE
        core.CONFIG_FILE = base / "claude-ui.json"
        self._zdot = os.environ.get("ZDOTDIR")
        os.environ["ZDOTDIR"] = str(self.home)

    def tearDown(self):
        core.config_dir = self._config_dir
        localmodel.config_dir = self._lm_config_dir
        settings.config_dir = self._s_config_dir
        core.CONFIG_FILE = self._cfg_file
        if self._zdot is None:
            os.environ.pop("ZDOTDIR", None)
        else:
            os.environ["ZDOTDIR"] = self._zdot
        self.tmp.cleanup()

    def configure(self, model="unsloth/Qwen3-14B-MLX-4bit", **kw):
        localmodel.local_config_set(kw.get("base_url", ""), model,
                                    kw.get("api_key", ""))

    def rc(self):
        return self.home / ".zshrc"

    def wrapper(self):
        return self.cfg / localmodel.LOCAL_WRAPPER_NAME


class Templates(Base):
    """The generated files themselves."""

    def test_wrapper_has_marker_exports_and_exec(self):
        self.configure()
        text = localmodel.local_wrapper_text()
        self.assertIn(localmodel.LOCAL_MARKER, text)
        for var in ("ANTHROPIC_BASE_URL='http://localhost:8000'",
                    "ANTHROPIC_AUTH_TOKEN='local'",
                    "ANTHROPIC_MODEL='unsloth/Qwen3-14B-MLX-4bit'",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL='unsloth/Qwen3-14B-MLX-4bit'",
                    "ANTHROPIC_SMALL_FAST_MODEL='unsloth/Qwen3-14B-MLX-4bit'",
                    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1",
                    "ANTHROPIC_CUSTOM_MODEL_OPTION='unsloth/Qwen3-14B-MLX-4bit'",
                    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME='unsloth/Qwen3-14B-MLX-4bit'",
                    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION='local model via oMLX'",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"):
            self.assertIn("export " + var, text)
        self.assertTrue(text.endswith('exec claude "$@"\n'))

    def test_api_key_becomes_the_auth_token(self):
        self.configure(api_key="sk-abc123")
        self.assertIn("ANTHROPIC_AUTH_TOKEN='sk-abc123'",
                      localmodel.local_wrapper_text())

    @unittest.skipUnless(shutil.which("sh"), "no sh on PATH")
    def test_wrapper_syntax_checks_with_sh(self):
        self.configure()
        p = self.cfg / "check.sh"
        p.write_text(localmodel.local_wrapper_text())
        r = subprocess.run(["sh", "-n", str(p)], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_zsh_function_quotes_the_wrapper_path(self):
        text = localmodel.local_zsh_text()
        self.assertIn(localmodel.LOCAL_MARKER, text)
        self.assertIn(f'"{self.wrapper()}" "$@"', text)


class Injection(Base):
    """Config values land inside single quotes in a shell script; anything
    that could escape them is refused at save time."""

    def test_model_rejects_shell_metacharacters(self):
        for bad in ("a'b", "a b", "a$(x)", "a`x`", 'a"b', "a\nb", "a;b"):
            with self.assertRaises(ValueError, msg=bad):
                localmodel.local_config_set("", bad, "")

    def test_api_key_rejects_only_quote_and_control_chars(self):
        # a key is server-generated: any printable ASCII except the one
        # character that escapes single quotes must round-trip
        for bad in ("a'b", "a\nb", "a\tb", "a\x00b", "café"):
            with self.assertRaises(ValueError, msg=bad):
                localmodel.local_config_set("", "m", bad)
        odd = 'sk!#$%^&*(){}[]|;<>?,"\\ key'
        localmodel.local_config_set("", "m", odd)
        self.assertEqual(localmodel.local_cfg()["api_key"], odd)
        # and it lands in the wrapper still inside intact single quotes
        self.assertIn(f"ANTHROPIC_AUTH_TOKEN='{odd}'",
                      localmodel.local_wrapper_text())

    def test_rejects_bad_urls(self):
        for bad in ("ftp://x", "localhost:8000", "http://a b", "http://a'b"):
            with self.assertRaises(ValueError, msg=bad):
                localmodel.local_config_set(bad, "m", "")

    def test_accepts_real_values(self):
        localmodel.local_config_set("http://localhost:8000",
                                    "unsloth/Qwen3-14B-MLX-4bit", "sk-abc_x")
        self.assertEqual(localmodel.local_cfg()["model"],
                         "unsloth/Qwen3-14B-MLX-4bit")


class Config(Base):

    def test_round_trip_and_default_base_url(self):
        self.configure()
        c = localmodel.local_cfg()
        self.assertEqual(c, {"base_url": "http://localhost:8000",
                             "model": "unsloth/Qwen3-14B-MLX-4bit",
                             "api_key": ""})
        self.assertTrue(json.loads(core.CONFIG_FILE.read_text())["local_model"])

    def test_trailing_slash_is_stripped(self):
        self.configure(base_url="http://127.0.0.1:9001/")
        self.assertEqual(localmodel.local_cfg()["base_url"],
                         "http://127.0.0.1:9001")

    def test_model_change_regenerates_an_installed_wrapper(self):
        self.configure()
        localmodel.local_apply()
        self.configure(model="mlx-community/other-model-4bit")
        self.assertIn("ANTHROPIC_MODEL='mlx-community/other-model-4bit'",
                      self.wrapper().read_text())

    def test_model_change_without_wrapper_writes_nothing(self):
        self.configure()
        self.configure(model="mlx-community/other-model-4bit")
        self.assertFalse(self.wrapper().exists())


class WrapperState(Base):

    def test_lifecycle_states(self):
        self.configure()
        self.assertEqual(localmodel.local_wrapper_state(), "none")
        localmodel.local_wrapper_write()
        self.assertEqual(localmodel.local_wrapper_state(), "current")
        self.assertEqual(self.wrapper().stat().st_mode & 0o777, 0o700)
        self.wrapper().write_text(
            f"# {localmodel.LOCAL_MARKER} old\nexec claude\n")
        self.assertEqual(localmodel.local_wrapper_state(), "stale")
        self.wrapper().write_text("#!/bin/sh\nexec claude\n")
        self.assertEqual(localmodel.local_wrapper_state(), "foreign")

    def test_foreign_wrapper_refused_without_force(self):
        self.configure()
        self.wrapper().write_text("mine\n")
        with self.assertRaises(ValueError):
            localmodel.local_wrapper_write()
        localmodel.local_wrapper_write(force=True)
        self.assertEqual(localmodel.local_wrapper_state(), "current")


class ApplyRemove(Base):

    def test_apply_without_model_raises(self):
        with self.assertRaises(ValueError):
            localmodel.local_apply()

    def test_apply_writes_everything_and_is_idempotent(self):
        self.configure()
        localmodel.local_apply()
        st = localmodel.local_state()
        self.assertTrue(st["installed"])
        rc_text = self.rc().read_text()
        self.assertEqual(rc_text.count(localmodel.LOCAL_BEGIN), 1)
        before = (self.wrapper().read_bytes(), rc_text)
        localmodel.local_apply()
        self.assertEqual((self.wrapper().read_bytes(),
                          self.rc().read_text()), before)

    def test_coexists_with_the_projects_block_and_removes_cleanly(self):
        other = (f"{projects.ZSHRC_BEGIN}\nsource x\n{projects.ZSHRC_END}\n")
        self.rc().write_text(other)
        self.configure()
        localmodel.local_apply()
        self.assertIn(projects.ZSHRC_BEGIN, self.rc().read_text())
        localmodel.local_remove()
        self.assertEqual(self.rc().read_text(), other)
        self.assertFalse(self.wrapper().exists())
        self.assertFalse((self.cfg / localmodel.LOCAL_ZSH_NAME).exists())
        # the config is user input and survives removal
        self.assertTrue(localmodel.local_cfg()["model"])

    def test_damaged_block_refused(self):
        self.configure()
        localmodel.local_apply()
        text = self.rc().read_text().replace(localmodel.LOCAL_END, "# gone")
        self.rc().write_text(text)
        with self.assertRaises(ValueError):
            localmodel.local_remove()

    def test_remove_leaves_foreign_files(self):
        self.configure()
        self.wrapper().write_text("mine\n")
        localmodel.local_remove()
        self.assertEqual(self.wrapper().read_text(), "mine\n")

    def test_state_has_the_piece_keys_and_no_network(self):
        st = localmodel.local_state()
        for k in ("id", "label", "desc", "installed", "detail", "removable",
                  "notes", "config", "target"):
            self.assertIn(k, st)
        self.assertEqual(st["id"], "local-model")
        self.assertFalse(st["installed"])


class Pricing(Base):

    def pricing(self):
        return core.read_cfg().get("pricing", {})

    def test_apply_adds_a_zero_override(self):
        self.configure()
        localmodel.local_apply()
        self.assertEqual(self.pricing()["unsloth/Qwen3-14B-MLX-4bit"], [0, 0])

    def test_model_change_migrates_it(self):
        self.configure()
        localmodel.local_apply()
        self.configure(model="mlx-community/other-model-4bit")
        self.assertEqual(self.pricing(),
                         {"mlx-community/other-model-4bit": [0, 0]})

    def test_remove_drops_it_only_while_still_zero(self):
        self.configure()
        localmodel.local_apply()
        localmodel.local_remove()
        self.assertNotIn("pricing", core.read_cfg())
        localmodel.local_apply()
        cfg = core.read_cfg()
        cfg["pricing"]["unsloth/Qwen3-14B-MLX-4bit"] = [1, 2]  # user repriced
        core.write_cfg(cfg)
        localmodel.local_remove()
        self.assertEqual(self.pricing()["unsloth/Qwen3-14B-MLX-4bit"], [1, 2])


class PickerEntry(Base):
    """The one settings.json write: the additive /model picker env pair."""

    MODEL = "unsloth/Qwen3-14B-MLX-4bit"

    def sjson(self):
        p = self.cfg / "settings.json"
        return json.loads(p.read_text()) if p.is_file() else {}

    def test_apply_writes_the_env_pair(self):
        self.configure()
        localmodel.local_apply()
        env = self.sjson()["env"]
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], self.MODEL)
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"],
                         localmodel.PICKER_DESC)

    def test_apply_patches_never_replaces(self):
        (self.cfg / "settings.json").write_text(
            '{"theme": "dark", "env": {"FOO": "bar"}}')
        self.configure()
        localmodel.local_apply()
        data = self.sjson()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"]["FOO"], "bar")

    def test_remove_drops_only_our_values_and_prunes(self):
        self.configure()
        localmodel.local_apply()
        localmodel.local_remove()
        self.assertNotIn("env", self.sjson())
        # a repointed entry is the user's and survives
        localmodel.local_apply()
        settings.settings_set("env.ANTHROPIC_CUSTOM_MODEL_OPTION", "other")
        localmodel.local_remove()
        self.assertEqual(self.sjson()["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION"],
                         "other")
        # the description we still owned is gone
        self.assertNotIn("ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
                         self.sjson()["env"])

    def test_model_change_moves_the_entry_when_installed(self):
        self.configure()
        localmodel.local_apply()
        self.configure(model="mlx-community/other-model-4bit")
        self.assertEqual(self.sjson()["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION"],
                         "mlx-community/other-model-4bit")
        st = localmodel.local_state()
        self.assertTrue(st["installed"])

    def test_missing_entry_reads_as_not_installed(self):
        self.configure()
        localmodel.local_apply()
        settings.settings_set("env.ANTHROPIC_CUSTOM_MODEL_OPTION", None)
        self.assertFalse(localmodel.local_state()["installed"])


class Suggestions(Base):

    def test_saved_model_feeds_the_settings_model_pickers(self):
        from claude_ui import settings
        self.configure()
        sugg = settings.suggest_state()
        for key in settings.MODEL_VALUED_KEYS:
            self.assertIn("unsloth/Qwen3-14B-MLX-4bit", sugg[key], key)

    def test_no_saved_model_suggests_nothing_extra(self):
        from claude_ui import settings
        sugg = settings.suggest_state()
        for vals in sugg.values():
            self.assertNotIn("unsloth/Qwen3-14B-MLX-4bit", vals)


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Probe(Base):

    def test_lists_models(self):
        body = json.dumps({"data": [{"id": "b"}, {"id": "a"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            r = localmodel.local_probe()
        self.assertTrue(r["ok"])
        self.assertEqual(r["models"], ["a", "b"])
        self.assertIn("2 models", r["detail"])

    def test_unreachable_is_soft(self):
        err = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            r = localmodel.local_probe()
        self.assertFalse(r["ok"])
        self.assertEqual(r["models"], [])

    def test_auth_failure_hints_at_the_key(self):
        err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            r = localmodel.local_probe()
        self.assertFalse(r["ok"])
        self.assertIn("API key", r["detail"])

    def test_non_json_is_soft(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_Resp(b"<html>")):
            r = localmodel.local_probe()
        self.assertFalse(r["ok"])

    def test_bad_url_never_probed(self):
        r = localmodel.local_probe("ftp://nope")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
