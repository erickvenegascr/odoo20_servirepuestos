import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bridge import access_jet as jet
from bridge.access_jet_cli import main


class AccessJetTests(unittest.TestCase):
    """Synthetic ODBC contract tests; not native Access/concurrency certification."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.canary = "private-canary};exclusive=yes;other={value"
        self.secret = self.root / "smb-password.txt"
        self.secret.write_text(self.canary)
        self.config = {"mdb_file": str(self.root / "Fixture.mdb"),
                       "smb_path": "smb://fixture.invalid/share/Fixture.mdb",
                       "smb_library": "/usr/lib/libsmbclient.so", "smb_user": "fixture-reader",
                       "smb_password_file": str(self.secret), "timeout_seconds": 3,
                       "numeric_properties": ["FixtureArticuloCounter"]}
        self.driver = MagicMock()
        self.driver.SQL_DATA_SOURCE_READ_ONLY = 25
        self.driver.SQL_DATABASE_NAME = 16
        self.driver.SQL_TXN_CAPABLE = 46
        self.connection = self.driver.connect.return_value
        self.connection.autocommit = False
        self.connection.messages = []
        self.mode = "Y"
        self.connection.getinfo.side_effect = lambda kind: {
            25: self.mode, 16: self.config["mdb_file"], 46: 2}[kind]
        self.cursor = self.connection.cursor.return_value
        self.cursor.rowcount = 1
        self.cursor.fetchmany.side_effect = [[("0010",)], [("0011",)]]
        self.driver_patch = patch.object(jet, "driver_module", return_value=self.driver)
        self.driver_patch.start()
        self.addCleanup(self.driver_patch.stop)

    def update(self, **kwargs):
        return jet.compare_and_set_numeric_property(
            self.config, "FixtureArticuloCounter", "0010", "0011", **kwargs)

    def assert_closed(self):
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(jet.AccessJetError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_default_simulation_is_read_only_and_closes(self):
        result = self.update()
        self.assertEqual(result["status"], "simulated")
        self.assertFalse(result["business_writes"])
        self.assertEqual(self.driver.connect.call_args.kwargs,
                         {"readonly": True, "autocommit": True, "timeout": 3})
        self.cursor.execute.assert_called_once_with(jet.READ_SQL, "FixtureArticuloCounter")
        self.connection.commit.assert_not_called()
        self.assert_closed()

    def test_apply_is_a_parameterized_compare_and_set(self):
        self.mode = "N"
        result = self.update(apply=True)
        self.assertEqual(result["status"], "committed")
        self.assertFalse(result["article_created"])
        self.assertIn(((jet.UPDATE_SQL, "0011", "FixtureArticuloCounter", "0010"),),
                      [tuple(call)[:1] for call in self.cursor.execute.call_args_list])
        self.connection.commit.assert_called_once_with()
        self.assert_closed()

    def test_stale_expected_value_does_not_write(self):
        self.mode = "N"
        self.cursor.fetchmany.side_effect = [[("0012",)]]
        self.assert_error("ACCESS_COUNTER_CONFLICT", self.update, apply=True)
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()
        self.assert_closed()

    def test_concurrent_change_is_not_overwritten_or_retried(self):
        self.mode = "N"
        self.cursor.rowcount = 0
        self.assert_error("ACCESS_COUNTER_CONFLICT_OR_UNKNOWN_ROWCOUNT", self.update, apply=True)
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.assert_closed()

    def test_unreliable_rowcount_never_commits(self):
        self.mode = "N"
        self.cursor.rowcount = -1
        self.assert_error("ACCESS_COUNTER_CONFLICT_OR_UNKNOWN_ROWCOUNT", self.update, apply=True)
        self.connection.commit.assert_not_called()
        self.assert_closed()

    def test_post_update_verification_failure_rolls_back(self):
        self.mode = "N"
        self.cursor.fetchmany.side_effect = [[("0010",)], [("0013",)]]
        self.assert_error("ACCESS_COUNTER_VERIFICATION_FAILED", self.update, apply=True)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.assert_closed()

    def test_lock_timeout_rolls_back_and_closes(self):
        self.mode = "N"
        self.cursor.execute.side_effect = [None, RuntimeError(self.canary)]
        error = self.assert_error("ACCESS_UPDATE_ROLLED_BACK", self.update, apply=True)
        self.assertNotIn(self.canary, str(error))
        self.connection.rollback.assert_called_once_with()
        self.assert_closed()

    def test_lost_commit_response_is_unknown_and_not_retried(self):
        self.mode = "N"
        self.connection.commit.side_effect = RuntimeError(self.canary)
        error = self.assert_error("ACCESS_RESULT_UNKNOWN", self.update, apply=True)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(self.cursor.execute.call_count, 3)
        self.assert_closed()

    def test_failed_rollback_after_dml_is_unknown(self):
        self.mode = "N"
        self.cursor.rowcount = 0
        self.connection.rollback.side_effect = RuntimeError(self.canary)
        error = self.assert_error("ACCESS_RESULT_UNKNOWN", self.update, apply=True)
        self.assertTrue(error.outcome_unknown)
        self.assert_closed()

    def test_driver_without_transactions_is_rejected_before_sql(self):
        self.mode = "N"
        self.connection.getinfo.side_effect = lambda kind: {
            25: "N", 16: self.config["mdb_file"], 46: 0}[kind]
        self.assert_error("ACCESS_TRANSACTIONS_REQUIRED", self.update, apply=True)
        self.cursor.execute.assert_not_called()
        self.connection.close.assert_called_once_with()

    def test_wrong_database_is_rejected_before_sql(self):
        self.connection.getinfo.side_effect = lambda kind: {25: "Y", 16: "/other/Fixture.mdb"}[kind]
        self.assert_error("ACCESS_DATABASE_MISMATCH", self.update)
        self.cursor.execute.assert_not_called()
        self.connection.close.assert_called_once_with()

    def test_smb_warning_is_fatal_before_dml(self):
        self.mode = "N"
        self.connection.messages = [("01000", "read write access without SMB channel can allow corruption")]
        self.assert_error("ACCESS_SMB_LOCKING_NOT_CONFIRMED", self.update, apply=True)
        self.cursor.execute.assert_not_called()
        self.connection.close.assert_called_once_with()

    def test_read_only_mode_cannot_silently_be_ignored(self):
        self.mode = "N"
        self.assert_error("ACCESS_MODE_NOT_CONFIRMED", self.update)
        self.cursor.execute.assert_not_called()
        self.connection.close.assert_called_once_with()

    def test_property_allowlist_cannot_be_bypassed(self):
        self.config["numeric_properties"] = []
        self.assert_error("ACCESS_PROPERTY_NOT_ALLOWED", self.update, apply=True)
        self.driver.connect.assert_not_called()

    def test_decrements_and_non_numeric_values_are_rejected_before_connect(self):
        for expected, replacement in [("10", "9"), ("10", "10"), ("True", "11"),
                                      ("10", "2147483648"), ("10", "11;DELETE")]:
            with self.subTest(expected=expected, replacement=replacement):
                with self.assertRaises(jet.AccessJetError):
                    jet.compare_and_set_numeric_property(self.config, "FixtureArticuloCounter",
                                                         expected, replacement, apply=True)
        self.driver.connect.assert_not_called()

    def test_connection_cannot_inject_exclusive_or_a_different_lock_file(self):
        connection = jet.connection_string(self.config, write=True)
        self.assertIn("exclusive={no}", connection)
        self.assertIn("smbauth={private-canary}};exclusive=yes;other={value}", connection)
        self.assertNotIn("lockfile=", connection.lower())
        self.config["lockfile"] = "/tmp/other.ldb"
        self.assert_error("ACCESS_INVALID_CONFIG", jet.connection_string, self.config)

    def test_smb_target_must_refer_to_the_same_file(self):
        self.config["smb_path"] = "smb://fixture.invalid/share/Another.mdb"
        self.assert_error("ACCESS_INVALID_SMB_TARGET", self.update)
        self.driver.connect.assert_not_called()

    def test_close_error_does_not_leave_the_connection_unclosed(self):
        self.cursor.close.side_effect = RuntimeError(self.canary)
        self.update()
        self.connection.close.assert_called_once_with()

    def test_inspection_redacts_unrelated_values_and_never_claims_counter_found(self):
        self.cursor.fetchmany.side_effect = [[("ArticuloSecuencial", "True"),
                                            ("UltArticulo", "123"),
                                            ("ArticuloSecret", self.canary)]]
        result = jet.inspect_numbering(self.config)
        self.assertFalse(result["counter_confirmed"])
        self.assertFalse(result["dao_properties_inspected"])
        self.assertFalse(result["business_writes"])
        self.assertEqual(result["candidates"][1]["value"], "123")
        self.assertNotIn(self.canary, json.dumps(result))
        self.assertEqual(len(result["candidates"]), 2)
        self.assert_closed()

    def test_cli_never_prints_driver_secrets(self):
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config))
        self.driver.connect.side_effect = RuntimeError(self.canary)
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = main(["--config-file", str(config_file), "inspect"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["code"], "ACCESS_CONNECTION_OR_QUERY_FAILED")
        self.assertFalse(result["automatic_retry_allowed"])
        self.assertNotIn(self.canary, output.getvalue())


if __name__ == "__main__":
    unittest.main()
