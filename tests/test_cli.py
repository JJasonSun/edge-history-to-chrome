import csv
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from edge_history_to_chrome import (
    STATE_FILE,
    UserError,
    _build_places_database,
    cleanup,
    main,
    prepare,
    status,
)


FIXED_NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def write_csv(path: Path, rows, headers=None):
    fieldnames = headers or ["DateTime", "NavigatedToUrl", "PageTitle"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "Firefox"
        self.csv = self.base / "history.csv"

    def sample_rows(self):
        return [
            {
                "DateTime": "2026-08-30T10:00:00.901Z",
                "NavigatedToUrl": "https://example.com/a",
                "PageTitle": "New title",
            },
            {
                "DateTime": "2026-08-29T09:00:00.222Z",
                "NavigatedToUrl": "https://example.com/a",
                "PageTitle": "Old title",
            },
            {
                "DateTime": "2026-08-28T08:00:00Z",
                "NavigatedToUrl": "http://example.org/",
                "PageTitle": "Example Org",
            },
            {
                "DateTime": "2026-08-27T07:00:00Z",
                "NavigatedToUrl": "edge://settings/",
                "PageTitle": "Settings",
            },
        ]

    def test_prepare_status_and_cleanup_without_existing_firefox(self):
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)

        self.assertEqual(state["visits"], 3)
        self.assertEqual(state["unique_urls"], 2)
        self.assertEqual(state["skipped_non_web"], 1)
        profile = self.root / "Profiles" / state["profile_name"]
        self.assertTrue((profile / STATE_FILE).is_file())
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((profile / STATE_FILE).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((profile / "places.sqlite").stat().st_mode), 0o600)
        stored_state = json.loads((profile / STATE_FILE).read_text(encoding="utf-8"))
        self.assertNotIn(str(self.csv), json.dumps(stored_state))
        self.assertNotIn("example.com", json.dumps(stored_state))

        connection = sqlite3.connect(profile / "places.sqlite")
        try:
            visits = connection.execute(
                "SELECT COUNT(*) FROM moz_historyvisits"
            ).fetchone()[0]
            self.assertEqual(visits, 3)
            place = connection.execute(
                "SELECT title, visit_count FROM moz_places WHERE url = ?",
                ("https://example.com/a",),
            ).fetchone()
            self.assertEqual(place, ("New title", 2))
        finally:
            connection.close()

        result = status(self.root)
        self.assertEqual(result[0]["visits"], 3)
        self.assertEqual(result[0]["integrity"], "ok")

        cleanup(self.root)
        self.assertFalse(self.root.exists())

    def test_existing_profiles_ini_is_restored_byte_for_byte(self):
        original = (
            b"[General]\r\nStartWithLastProfile=1\r\nVersion=2\r\n\r\n"
            b"[Profile0]\r\nName=Personal\r\nIsRelative=1\r\nPath=Profiles/personal\r\n"
        )
        (self.root / "Profiles" / "personal").mkdir(parents=True)
        root_mode = stat.S_IMODE(self.root.stat().st_mode)
        profiles_ini = self.root / "profiles.ini"
        profiles_ini.write_bytes(original)
        os.chmod(profiles_ini, 0o640)
        write_csv(self.csv, self.sample_rows())

        state = prepare(self.csv, self.root, now=FIXED_NOW)
        installed = profiles_ini.read_text(encoding="utf-8")
        self.assertIn("[Profile1]", installed)
        self.assertIn(state["profile_name"], installed)

        cleanup(self.root)
        self.assertEqual(profiles_ini.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(profiles_ini.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), root_mode)
        self.assertTrue((self.root / "Profiles" / "personal").is_dir())

    def test_bad_headers_leave_no_files(self):
        write_csv(
            self.csv,
            [{"Date": "2026-08-30", "URL": "https://example.com"}],
            headers=["Date", "URL"],
        )
        with self.assertRaises(UserError):
            prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertFalse(self.root.exists())

    def test_timestamp_requires_a_timezone(self):
        rows = self.sample_rows()
        rows[0]["DateTime"] = "2026-08-30T10:00:00"
        write_csv(self.csv, rows)
        with self.assertRaises(UserError):
            prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertFalse(self.root.exists())

    def test_bad_timestamp_is_not_echoed_to_stderr(self):
        rows = self.sample_rows()
        rows[0]["DateTime"] = "https://private.example/should-not-print"
        write_csv(self.csv, rows)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    "prepare",
                    str(self.csv),
                    "--firefox-root",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("private.example", stderr.getvalue())
        self.assertFalse(self.root.exists())

    def test_malformed_url_is_filtered(self):
        rows = [self.sample_rows()[0]]
        rows.append(
            {
                "DateTime": "2026-08-30T11:00:00Z",
                "NavigatedToUrl": "http://[broken",
                "PageTitle": "Private title",
            }
        )
        write_csv(self.csv, rows)
        summary = prepare(self.csv, self.root, dry_run=True, now=FIXED_NOW)
        self.assertEqual(summary["visits"], 1)
        self.assertEqual(summary["skipped_non_web"], 1)

    def test_non_contiguous_profile_sections_are_left_unchanged(self):
        original = (
            b"[Profile0]\nName=One\nIsRelative=1\nPath=Profiles/one\n\n"
            b"[Profile2]\nName=Three\nIsRelative=1\nPath=Profiles/three\n"
        )
        self.root.mkdir()
        profiles_ini = self.root / "profiles.ini"
        profiles_ini.write_bytes(original)
        write_csv(self.csv, self.sample_rows())
        with self.assertRaises(UserError):
            prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertEqual(profiles_ini.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [profiles_ini])

    def test_profile_build_failure_rolls_back_created_files(self):
        write_csv(self.csv, self.sample_rows())
        with patch(
            "edge_history_to_chrome._build_places_database",
            side_effect=OSError("synthetic failure"),
        ):
            with self.assertRaises(OSError):
                prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertFalse(self.root.exists())

    def test_profiles_ini_change_during_prepare_is_preserved(self):
        self.root.mkdir()
        profiles_ini = self.root / "profiles.ini"
        original = b"[Profile0]\nName=Personal\nIsRelative=1\nPath=Profiles/personal\n"
        changed = original + b"\n# changed by Firefox\n"
        profiles_ini.write_bytes(original)
        write_csv(self.csv, self.sample_rows())

        def build_then_change(path, parsed):
            _build_places_database(path, parsed)
            profiles_ini.write_bytes(changed)

        with patch(
            "edge_history_to_chrome._build_places_database",
            side_effect=build_then_change,
        ):
            with self.assertRaises(UserError):
                prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertEqual(profiles_ini.read_bytes(), changed)
        self.assertFalse(any(self.root.glob("profiles.ini.edge-history-to-chrome.*.bak")))
        self.assertFalse(any((self.root / "Profiles").glob("edge-history-to-chrome-*")))

    def test_backup_name_collision_does_not_delete_existing_file(self):
        self.root.mkdir()
        (self.root / "profiles.ini").write_text(
            "[Profile0]\nName=Personal\nIsRelative=1\nPath=Profiles/personal\n",
            encoding="utf-8",
        )
        write_csv(self.csv, self.sample_rows())
        collision = {"path": None}

        def create_collision(path, _data, _mode):
            collision["path"] = path
            path.write_bytes(b"pre-existing backup")
            raise FileExistsError(path)

        with patch(
            "edge_history_to_chrome._write_exclusive",
            side_effect=create_collision,
        ):
            with self.assertRaises(FileExistsError):
                prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertIsNotNone(collision["path"])
        self.assertEqual(collision["path"].read_bytes(), b"pre-existing backup")

    def test_interrupt_after_profiles_replace_restores_original_file(self):
        self.root.mkdir()
        profiles_ini = self.root / "profiles.ini"
        original = b"[Profile0]\nName=Personal\nIsRelative=1\nPath=Profiles/personal\n"
        profiles_ini.write_bytes(original)
        write_csv(self.csv, self.sample_rows())

        from edge_history_to_chrome import _atomic_write as real_atomic_write

        interrupted = {"value": False}

        def interrupt_after_replace(path, data, mode):
            real_atomic_write(path, data, mode)
            if path == profiles_ini and not interrupted["value"]:
                interrupted["value"] = True
                raise KeyboardInterrupt()

        with patch(
            "edge_history_to_chrome._atomic_write",
            side_effect=interrupt_after_replace,
        ):
            with self.assertRaises(KeyboardInterrupt):
                prepare(self.csv, self.root, now=FIXED_NOW)
        self.assertEqual(profiles_ini.read_bytes(), original)
        self.assertFalse(any((self.root / "Profiles").glob("edge-history-to-chrome-*")))
        self.assertFalse(any(self.root.glob("profiles.ini.edge-history-to-chrome.*.bak")))

    def test_second_prepare_is_refused(self):
        write_csv(self.csv, self.sample_rows())
        prepare(self.csv, self.root, now=FIXED_NOW)
        with self.assertRaises(UserError):
            prepare(self.csv, self.root, now=FIXED_NOW)
        cleanup(self.root)

    def test_cleanup_preserves_later_profiles_ini_changes(self):
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)
        profiles_ini = self.root / "profiles.ini"
        with profiles_ini.open("ab") as handle:
            handle.write(b"\n# added later\n")

        cleanup(self.root)
        remaining = profiles_ini.read_text(encoding="utf-8")
        self.assertIn("# added later", remaining)
        self.assertNotIn(state["profile_name"], remaining)

    def test_cleanup_refuses_an_altered_managed_block(self):
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)
        profiles_ini = self.root / "profiles.ini"
        text = profiles_ini.read_text(encoding="utf-8")
        profiles_ini.write_text(
            text.replace(state["relative_profile"], "Profiles/changed"),
            encoding="utf-8",
        )

        with self.assertRaises(UserError):
            cleanup(self.root)
        self.assertTrue((self.root / "Profiles" / state["profile_name"]).is_dir())

    def test_tampered_backup_name_cannot_escape_firefox_root(self):
        (self.root / "Profiles" / "personal").mkdir(parents=True)
        (self.root / "profiles.ini").write_text(
            "[Profile0]\nName=Personal\nIsRelative=1\nPath=Profiles/personal\n",
            encoding="utf-8",
        )
        victim = self.base / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)
        profile = self.root / "Profiles" / state["profile_name"]
        state_path = profile / STATE_FILE
        stored = json.loads(state_path.read_text(encoding="utf-8"))
        stored["backup_name"] = "../victim.txt"
        state_path.write_text(json.dumps(stored), encoding="utf-8")

        with self.assertRaises(UserError):
            cleanup(self.root)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertTrue(profile.is_dir())

    def test_modified_backup_blocks_cleanup_before_profiles_change(self):
        (self.root / "Profiles" / "personal").mkdir(parents=True)
        (self.root / "profiles.ini").write_text(
            "[Profile0]\nName=Personal\nIsRelative=1\nPath=Profiles/personal\n",
            encoding="utf-8",
        )
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)
        backup = self.root / state["backup_name"]
        backup.write_bytes(b"replacement")
        profiles_ini = self.root / "profiles.ini"
        before = profiles_ini.read_bytes()

        with self.assertRaises(UserError):
            cleanup(self.root)
        self.assertEqual(profiles_ini.read_bytes(), before)
        self.assertEqual(backup.read_bytes(), b"replacement")

    def test_unexpected_profile_file_blocks_cleanup_before_ini_change(self):
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)
        profile = self.root / "Profiles" / state["profile_name"]
        (profile / "prefs.js").write_text("user_pref('x', true);", encoding="utf-8")
        profiles_ini = self.root / "profiles.ini"
        before = profiles_ini.read_bytes()

        with self.assertRaises(UserError):
            cleanup(self.root)
        self.assertEqual(profiles_ini.read_bytes(), before)
        self.assertTrue((profile / "prefs.js").is_file())

    def test_cleanup_recovers_after_profiles_ini_was_already_removed(self):
        write_csv(self.csv, self.sample_rows())
        state = prepare(self.csv, self.root, now=FIXED_NOW)
        profile = self.root / "Profiles" / state["profile_name"]
        (self.root / "profiles.ini").unlink()
        (profile / "places.sqlite").unlink()
        (profile / "compatibility.ini").unlink()

        cleanup(self.root)
        self.assertFalse(self.root.exists())

    def test_real_profile_name_is_not_treated_as_managed(self):
        profile = self.root / "Profiles" / "personal.default-release"
        profile.mkdir(parents=True)
        (profile / STATE_FILE).write_text("{}", encoding="utf-8")
        self.assertEqual(status(self.root), [])
        with self.assertRaises(UserError):
            cleanup(self.root)
        self.assertTrue(profile.is_dir())

    def test_symlinked_state_file_is_rejected(self):
        run_id = "20260831T120000Z-abcdef"
        profile = self.root / "Profiles" / f"edge-history-to-chrome-{run_id}"
        profile.mkdir(parents=True)
        external = self.base / "external-state.json"
        external.write_text("{}", encoding="utf-8")
        (profile / STATE_FILE).symlink_to(external)

        with self.assertRaises(UserError):
            status(self.root)
        self.assertEqual(external.read_text(encoding="utf-8"), "{}")

    def test_dry_run_writes_nothing(self):
        write_csv(self.csv, self.sample_rows())
        summary = prepare(self.csv, self.root, dry_run=True, now=FIXED_NOW)
        self.assertEqual(summary["visits"], 3)
        self.assertFalse(self.root.exists())

    def test_cli_prints_import_page_without_urls(self):
        write_csv(self.csv, self.sample_rows())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    "prepare",
                    str(self.csv),
                    "--firefox-root",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("chrome://settings/importData", stdout.getvalue())
        self.assertNotIn("example.com", stdout.getvalue())
        cleanup(self.root)


if __name__ == "__main__":
    unittest.main()
