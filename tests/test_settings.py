import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import settings


class SettingsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "telemetry"
        # settings live beside the DB; point $TOKEN_TELEMETRY_DB at a file in
        # the telemetry dir so settings_path() resolves under our tmp tree.
        self._prev = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.dir / "usage.db")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TOKEN_TELEMETRY_DB", None)
        else:
            os.environ["TOKEN_TELEMETRY_DB"] = self._prev
        self.tmp.cleanup()


class TestPathResolution(SettingsBase):
    def test_settings_sit_beside_the_db(self):
        self.assertEqual(settings.settings_path(),
                         self.dir / "settings.json")
        self.assertEqual(settings.telemetry_dir(), self.dir)


class TestReadTolerance(SettingsBase):
    def test_absent_file_reads_as_empty(self):
        self.assertEqual(settings.read_settings(), {})
        self.assertIsNone(settings.current_owner_id())
        self.assertIsNone(settings.current_user())

    def test_corrupt_json_never_raises(self):
        self.dir.mkdir(parents=True)
        settings.settings_path().write_text("{ this is not json")
        self.assertEqual(settings.read_settings(), {})
        self.assertIsNone(settings.current_owner_id())

    def test_non_object_top_level_reads_as_empty(self):
        self.dir.mkdir(parents=True)
        settings.settings_path().write_text("[1, 2, 3]")
        self.assertEqual(settings.read_settings(), {})
        self.assertIsNone(settings.current_owner_id())

    def test_partial_user_block_is_no_identity(self):
        # a 'user' block missing its uuid (or with an empty one) reads as no
        # identity rather than a half-identity.
        for user in ({}, {"full_name": "Ada"}, {"uuid": ""}, {"uuid": 5}):
            settings.write_settings({"user": user, "active_backend": "local"})
            self.assertIsNone(settings.current_owner_id(), user)
            self.assertIsNone(settings.current_user(), user)

    def test_user_not_a_dict_is_no_identity(self):
        settings.write_settings({"user": "nope"})
        self.assertIsNone(settings.current_owner_id())


class TestWritePermissions(SettingsBase):
    def test_file_is_written_0600(self):
        settings.write_settings({"user": {"uuid": "u", "full_name": "Ada"}})
        mode = stat.S_IMODE(settings.settings_path().stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_existing_loose_file_is_tightened_to_0600(self):
        self.dir.mkdir(parents=True)
        p = settings.settings_path()
        p.write_text("{}")
        os.chmod(p, 0o644)
        settings.write_settings({"user": {"uuid": "u", "full_name": "Ada"}})
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_write_creates_the_telemetry_dir(self):
        self.assertFalse(self.dir.exists())
        settings.write_settings({"active_backend": "local"})
        self.assertTrue(self.dir.exists())

    def test_no_tmp_files_left_behind(self):
        settings.write_settings({"active_backend": "local"})
        leftovers = [p.name for p in self.dir.iterdir()
                     if ".tmp" in p.name]
        self.assertEqual(leftovers, [])


class TestEnsureIdentity(SettingsBase):
    def test_first_call_mints_a_uuid_and_persists(self):
        s, minted = settings.ensure_identity("Ada Lovelace")
        self.assertTrue(minted)
        uid = s["user"]["uuid"]
        self.assertTrue(uid)
        # a real uuid4 string
        import uuid as _uuid
        self.assertEqual(str(_uuid.UUID(uid)), uid)
        self.assertEqual(s["user"]["full_name"], "Ada Lovelace")
        self.assertEqual(s["active_backend"], "local")
        # persisted, 0600
        on_disk = json.loads(settings.settings_path().read_text())
        self.assertEqual(on_disk["user"]["uuid"], uid)
        self.assertEqual(stat.S_IMODE(settings.settings_path().stat().st_mode),
                         0o600)

    def test_uuid_is_stable_across_reruns(self):
        s1, minted1 = settings.ensure_identity("Ada")
        s2, minted2 = settings.ensure_identity("Ada")
        self.assertTrue(minted1)
        self.assertFalse(minted2)  # never re-mints
        self.assertEqual(s1["user"]["uuid"], s2["user"]["uuid"])

    def test_name_update_keeps_the_uuid(self):
        s1, _ = settings.ensure_identity("Ada")
        s2, minted = settings.ensure_identity("Ada Lovelace")
        self.assertFalse(minted)
        self.assertEqual(s1["user"]["uuid"], s2["user"]["uuid"])
        self.assertEqual(s2["user"]["full_name"], "Ada Lovelace")

    def test_existing_backend_preserved(self):
        settings.write_settings({"active_backend": "supabase"})
        s, _ = settings.ensure_identity("Ada")
        # ensure_identity must not clobber a non-default backend a later phase
        # may have written.
        self.assertEqual(s["active_backend"], "supabase")


class TestNeverPrompts(unittest.TestCase):
    def test_settings_source_has_no_interactive_calls(self):
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "settings.py").read_text()
        self.assertNotIn("input(", src)
        self.assertNotIn("getpass", src)


if __name__ == "__main__":
    unittest.main()
