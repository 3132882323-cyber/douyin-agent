from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from version import AGENT_VERSION


ROOT = Path(__file__).resolve().parent.parent
MACOS_TOOLS = ROOT / "tools" / "macos"
TRUST_HELPER_PATH = MACOS_TOOLS / "initialize_local_api_trust.py"
_TRUST_HELPER_SPEC = importlib.util.spec_from_file_location(
    "dian_agent_macos_trust_helper", TRUST_HELPER_PATH
)
assert _TRUST_HELPER_SPEC is not None and _TRUST_HELPER_SPEC.loader is not None
TRUST_HELPER = importlib.util.module_from_spec(_TRUST_HELPER_SPEC)
_TRUST_HELPER_SPEC.loader.exec_module(TRUST_HELPER)


class MacOSReleaseContractTests(unittest.TestCase):
    def test_release_versions_match(self) -> None:
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(AGENT_VERSION, manifest["version"])

    def test_installers_use_user_launchagent_without_gatekeeper_bypass(self) -> None:
        scripts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in MACOS_TOOLS.glob("*")
            if path.suffix in {".sh", ".command"}
        )
        self.assertIn("com.dianagent.agent", scripts)
        self.assertIn("Library/Application Support/DianAgent", scripts)
        self.assertIn("KeepAlive", scripts)
        self.assertIn("PLIST_TEMP", scripts)
        self.assertIn("chrome://extensions", scripts)
        self.assertIn("edge://extensions", scripts)
        self.assertNotIn("sudo ", scripts)
        self.assertNotIn("spctl --master-disable", scripts)
        self.assertNotIn("xattr -d", scripts)

    def test_source_installer_is_self_testing_and_version_exact(self) -> None:
        source = (MACOS_TOOLS / "install_dian_agent_source.command").read_text(encoding="utf-8")
        self.assertIn("DIAN_AGENT_SELF_TEST=1", source)
        self.assertIn('DIAN_AGENT_INSTALL_ROOT="$SELF_TEST_ROOT/install"', source)
        self.assertIn('DIAN_AGENT_DATA_DIR="$SELF_TEST_ROOT/data"', source)
        self.assertIn('DIAN_AGENT_LOG_DIR="$SELF_TEST_ROOT/logs"', source)
        self.assertIn('/bin/rm -rf "$SELF_TEST_ROOT"', source)
        self.assertIn("requirements-agent.txt", source)
        self.assertIn('source "$VERIFY_HELPER"', source)
        self.assertIn('dian_verify_local_api "$TRUST_RECEIPT" "$VERSION" 60', source)
        self.assertIn("launchctl bootstrap", source)
        before_transaction = source[: source.index('dian_install_transaction_begin "$INSTALL_ROOT"')]
        self.assertNotIn('"$INSTALL_ROOT/tools/macos"', before_transaction)
        self.assertIn('"$INSTALL_ROOT/tools"', before_transaction)
        self.assertIn('/bin/mkdir -m 700 "$STAGE"', source)

    def test_both_installers_provision_local_api_trust_before_agent_start(self) -> None:
        native = (MACOS_TOOLS / "install_dian_agent.command").read_text(encoding="utf-8")
        native_provision = native.index("--initialize-local-api-trust")
        self.assertLess(native_provision, native.index("launchctl bootstrap"))
        self.assertLess(native_provision, native.index('/bin/cp -f "$SOURCE_ROOT/app/DianAgent"'))
        self.assertLess(
            native_provision,
            native.index('dian_atomic_directory_commit "$INSTALL_ROOT" "$APP_STAGE"'),
        )
        self.assertLess(native_provision, native.index('current-version.txt"'))
        self.assertIn('"$SOURCE_ROOT/app/DianAgent" --initialize-local-api-trust', native)
        self.assertIn('"$SOURCE_ROOT/extension/manifest.json" "$INSTALL_ROOT"', native)
        self.assertIn('TRUST_RECEIPT="$("$SOURCE_ROOT/app/DianAgent"', native)
        self.assertIn('dian_verify_local_api "$TRUST_RECEIPT" "$VERSION" 40', native)
        self.assertGreater(native.index("dian_verify_local_api"), native.index("launchctl bootstrap"))
        self.assertNotIn("command -v python3", native)
        self.assertNotIn("$PYTHON_BIN", native)

        source = (MACOS_TOOLS / "install_dian_agent_source.command").read_text(
            encoding="utf-8"
        )
        source_provision = source.index("initialize_local_api_trust.py", source.index("# Provision"))
        self.assertLess(source_provision, source.index("DIAN_AGENT_SELF_TEST=1"))
        self.assertLess(source_provision, source.index("launchctl bootstrap"))
        self.assertLess(
            source_provision,
            source.index('dian_atomic_directory_commit "$INSTALL_ROOT" "$STAGE"'),
        )
        self.assertLess(source_provision, source.index('print -r -- "$VERSION" > "$VERSION_TEMP"'))
        self.assertLess(source_provision, source.index('/bin/mv -f "$PLIST_TEMP" "$PLIST_PATH"'))
        self.assertIn('--manifest "$SOURCE_ROOT/extension/manifest.json"', source)
        self.assertIn('--install-root "$INSTALL_ROOT"', source)
        self.assertIn("--json", source)
        self.assertGreater(source.index("dian_verify_local_api"), source.index("launchctl bootstrap"))

        receiver = (ROOT / "bridge" / "http_receiver.py").read_text(encoding="utf-8")
        dispatch = receiver.index("if __name__ == \"__main__\" and sys.argv[1:2] in")
        self.assertLess(dispatch, receiver.index("logging.basicConfig"))
        self.assertIn("provision_local_api_trust(arguments[2], arguments[1])", receiver)
        self.assertIn("repair_local_api_trust(arguments[2], arguments[1])", receiver)
        self.assertIn('["--repair-local-api-trust"]', receiver)
        self.assertIn('"agent_version": AGENT_VERSION', receiver)

        source_helper = (MACOS_TOOLS / "initialize_local_api_trust.py").read_text(encoding="utf-8")
        self.assertIn('"agent_version": AGENT_VERSION', source_helper)

        packager = (ROOT / "tools" / "build_macos_source_release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('"initialize_local_api_trust.py"', packager)
        self.assertIn('"verify_local_api.sh"', packager)
        self.assertIn('"atomic_directory_update.sh"', packager)
        self.assertIn('"recovery_bootstrap.sh"', packager)

    def test_launchagent_uses_stable_recovery_bootstrap_before_tree_switches(self) -> None:
        helper = (MACOS_TOOLS / "atomic_directory_update.sh").read_text(encoding="utf-8")
        bootstrap = (MACOS_TOOLS / "recovery_bootstrap.sh").read_text(encoding="utf-8")
        native_builder = (MACOS_TOOLS / "build_release.sh").read_text(encoding="utf-8")
        uninstall = (MACOS_TOOLS / "uninstall_dian_agent.command").read_text(
            encoding="utf-8"
        )
        self.assertIn("dian_install_bootstrap_update()", helper)
        self.assertIn("dian_install_prepare_stable_plist_migration()", helper)
        self.assertIn("launchagent.pre-transaction.plist", helper)
        self.assertIn("initial_plist_state=committed", helper)
        self.assertIn('plist_state "$initial_plist_state"', helper)
        self.assertIn("atomic_directory_update.sh recovery_bootstrap.sh", helper)
        self.assertIn('"$root_abs/bootstrap"', helper)
        self.assertIn("prepared|switching|verified", bootstrap)
        self.assertIn('exec "$LAUNCHER"', bootstrap)
        self.assertIn("recovery_bootstrap.sh", native_builder)
        self.assertIn('"$INSTALL_ROOT/bootstrap"', uninstall)

        for name in ("install_dian_agent.command", "install_dian_agent_source.command"):
            installer = (MACOS_TOOLS / name).read_text(encoding="utf-8")
            bootstrap_install = installer.index("dian_install_bootstrap_update")
            plist_migration = installer.index(
                "dian_install_prepare_stable_plist_migration"
            )
            transaction = installer.index('dian_install_transaction_begin "$INSTALL_ROOT"')
            plist_commit = installer.index(
                'dian_install_transaction_mark "$INSTALL_ROOT" plist committed'
            )
            app_switch = installer.index(
                'dian_install_transaction_mark "$INSTALL_ROOT" app committing'
            )
            tools_switch = installer.index(
                'dian_install_transaction_mark "$INSTALL_ROOT" tools committing'
            )
            self.assertLess(bootstrap_install, transaction)
            self.assertLess(plist_migration, transaction)
            self.assertLess(transaction, plist_commit)
            self.assertLess(plist_commit, app_switch)
            self.assertLess(plist_commit, tools_switch)
            self.assertIn(
                "$INSTALL_ROOT/bootstrap/recovery_bootstrap.sh", installer
            )

    def test_macos_updates_stage_validate_and_keep_a_recoverable_previous_tree(self) -> None:
        helper = (MACOS_TOOLS / "atomic_directory_update.sh").read_text(encoding="utf-8")
        native = (MACOS_TOOLS / "install_dian_agent.command").read_text(encoding="utf-8")
        source = (MACOS_TOOLS / "install_dian_agent_source.command").read_text(
            encoding="utf-8"
        )

        self.assertIn('local stage="$target_parent/.${target_name}.stage.$$"', helper)
        self.assertIn('local previous="$target_parent/.${target_name}.previous"', helper)
        self.assertIn('[[ "${prepared:h:A}" == "$target_parent"', helper)
        self.assertIn('[[ "$target_parent" == "$root_abs" ||', helper)
        self.assertLess(
            helper.index('"$validator" "$resolved_prepared"'),
            helper.index('/bin/mv "$target" "$previous"'),
        )
        self.assertLess(
            helper.index('/usr/bin/rsync -a --delete "${source:A}/" "$stage/"'),
            helper.index('dian_atomic_directory_commit "$install_root" "$stage"'),
        )
        self.assertIn('/bin/mv "$previous" "$target"', helper)
        self.assertIn("dian_tree_has_no_symlinks", helper)
        self.assertIn('/usr/bin/find "${tree:A}" -type l -print -quit', helper)
        self.assertLess(
            helper.index('dian_tree_has_no_symlinks "$source"'),
            helper.index('/usr/bin/rsync -a --delete "${source:A}/" "$stage/"'),
        )
        self.assertIn('dian_tree_has_no_symlinks "$resolved_prepared"', helper)
        self.assertNotIn(
            '/usr/bin/rsync -a --delete "$SOURCE_ROOT/extension/" "$INSTALL_ROOT/extension-current/"',
            native + source,
        )
        self.assertNotIn(
            '/usr/bin/rsync -a --delete "$SOURCE_ROOT/tools/macos/" "$INSTALL_ROOT/tools/macos/"',
            native + source,
        )
        for installer in (native, source):
            self.assertIn('source "$ATOMIC_UPDATE_HELPER"', installer)
            self.assertIn('dian_install_lock_acquire "$INSTALL_ROOT" 30', installer)
            self.assertIn("trap 'dian_installer_cleanup $?' EXIT", installer)
            self.assertLess(
                installer.index('dian_install_transaction_begin "$INSTALL_ROOT"'),
                installer.index('dian_atomic_directory_commit "$INSTALL_ROOT"'),
            )
            self.assertIn(
                'dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/extension"',
                installer,
            )
            self.assertIn(
                'dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/tools/macos"',
                installer,
            )
            self.assertLess(
                installer.index(
                    'dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/tools/macos"'
                ),
                installer.index(
                    'dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/extension"'
                ),
            )
            self.assertIn('print -r -- "$VERSION" > "$VERSION_TEMP"', installer)
            self.assertIn('/bin/mv -f "$VERSION_TEMP" "$INSTALL_ROOT/current-version.txt"', installer)

    def test_macos_install_repair_launch_and_uninstall_share_a_crash_recoverable_lock(self) -> None:
        helper = (MACOS_TOOLS / "atomic_directory_update.sh").read_text(encoding="utf-8")
        self.assertIn("dian_install_lock_acquire()", helper)
        self.assertIn("dian_install_lock_release()", helper)
        self.assertIn('/bin/kill -0 "$owner_pid"', helper)
        self.assertIn("dian_process_fingerprint", helper)
        self.assertIn(".install-operation.stale", helper)

        repair = (MACOS_TOOLS / "repair_dian_agent.command").read_text(encoding="utf-8")
        uninstall = (MACOS_TOOLS / "uninstall_dian_agent.command").read_text(encoding="utf-8")
        launcher = (MACOS_TOOLS / "launch_agent.sh").read_text(encoding="utf-8")
        for operation in (repair, uninstall):
            self.assertIn('dian_install_lock_acquire "$INSTALL_ROOT" 30', operation)
            self.assertIn(
                'dian_install_cleanup_orphan_stages "$INSTALL_ROOT" "$PLIST_PATH"',
                operation,
            )
        self.assertIn("trap 'dian_install_lock_release' EXIT", uninstall)
        self.assertIn('dian_install_transaction_recover "$INSTALL_ROOT"', uninstall)
        self.assertLess(uninstall.index("launchctl bootout"), uninstall.index("launchctl print"))
        self.assertIn(".repair-pending", uninstall)
        self.assertIn("dian_durable_sync", uninstall)
        self.assertIn("trap 'repair_cleanup' EXIT", repair)
        self.assertIn("dian_install_lock_release", repair)
        installers = (
            (MACOS_TOOLS / "install_dian_agent.command").read_text(encoding="utf-8"),
            (MACOS_TOOLS / "install_dian_agent_source.command").read_text(encoding="utf-8"),
        )
        self.assertGreater(
            repair.rindex("dian_install_lock_release || fail"),
            repair.index("dian_install_transaction_verify_job"),
        )
        for installer in installers:
            self.assertGreater(
                installer.index("dian_install_lock_release || fail"),
                installer.index("dian_install_transaction_verify_job"),
            )
            self.assertGreater(
                installer.index("dian_install_transaction_commit"),
                installer.index("dian_install_transaction_verify_job"),
            )
        self.assertIn(".install-operation.lock", launcher)
        self.assertIn("receipt_is_live()", launcher)
        self.assertIn('/bin/kill -0 "$receipt_pid"', launcher)
        self.assertIn("expected_fingerprint", launcher)
        self.assertIn('AUTHORIZED_VERSION" == "$VERSION', launcher)
        self.assertIn("exit 75", launcher)

    def test_macos_cross_tree_transaction_is_durable_recoverable_and_health_gated(self) -> None:
        helper = (MACOS_TOOLS / "atomic_directory_update.sh").read_text(encoding="utf-8")
        repair = (MACOS_TOOLS / "repair_dian_agent.command").read_text(encoding="utf-8")
        installers = [
            (MACOS_TOOLS / "install_dian_agent.command").read_text(encoding="utf-8"),
            (MACOS_TOOLS / "install_dian_agent_source.command").read_text(encoding="utf-8"),
        ]

        self.assertIn('local transaction_dir="$root_abs/.install-transaction"', helper)
        self.assertIn('local stage="$root_abs/.install-transaction.stage.$$.$RANDOM"', helper)
        self.assertIn('/bin/mv "$stage" "$transaction_dir"', helper)
        self.assertIn("current-version.previous", helper)
        self.assertIn("launchagent.previous.plist", helper)
        self.assertIn("old_job_loaded", helper)
        self.assertIn("old_job_running", helper)
        self.assertIn("dian_install_transaction_recover()", helper)
        self.assertIn("dian_install_transaction_verify_job()", helper)
        self.assertIn("state = running", helper)
        self.assertIn(
            '"$command_line" == "$expected_runtime" || "$command_line" == "$expected_runtime "*',
            helper,
        )
        self.assertIn('dian_install_transaction_recover "$INSTALL_ROOT"', repair)

        fault_points = (
            "after-app",
            "after-tools",
            "after-extension",
            "after-pointer",
            "after-plist",
            "after-bootstrap",
            "after-kickstart",
            "after-health",
        )
        for installer in installers:
            begin = installer.index('dian_install_transaction_begin "$INSTALL_ROOT"')
            first_switch = installer.index('dian_atomic_directory_commit "$INSTALL_ROOT"')
            verify = installer.index("dian_install_transaction_verify_job")
            commit = installer.index('dian_install_transaction_commit "$INSTALL_ROOT"')
            release = installer.index("dian_install_lock_release || fail")
            self.assertLess(begin, first_switch)
            self.assertLess(verify, commit)
            self.assertLess(commit, release)
            self.assertIn('dian_install_transaction_recover "$INSTALL_ROOT"', installer)
            for point in fault_points:
                self.assertIn(f"dian_install_fault {point}", installer)

    def test_macos_transaction_security_contract_is_fail_closed_and_durable(self) -> None:
        helper = (MACOS_TOOLS / "atomic_directory_update.sh").read_text(encoding="utf-8")
        launcher = (MACOS_TOOLS / "launch_agent.sh").read_text(encoding="utf-8")
        repair = (MACOS_TOOLS / "repair_dian_agent.command").read_text(encoding="utf-8")
        installers = [
            (MACOS_TOOLS / "install_dian_agent.command").read_text(encoding="utf-8"),
            (MACOS_TOOLS / "install_dian_agent_source.command").read_text(
                encoding="utf-8"
            ),
        ]

        durable = helper[
            helper.index("dian_durable_sync()") : helper.index("dian_process_fingerprint()")
        ]
        self.assertIn("/bin/sync", durable)
        write_field = helper[
            helper.index("dian_transaction_write_field()") : helper.index(
                "dian_transaction_read_field()"
            )
        ]
        self.assertIn('/usr/bin/mktemp "$transaction_dir/.${2}.tmp.XXXXXX"', write_field)
        self.assertLess(write_field.index("mktemp"), write_field.index('/bin/mv -f "$temporary"'))
        self.assertLess(
            write_field.index('/bin/mv -f "$temporary"'),
            write_field.index("dian_durable_sync"),
        )

        begin = helper[
            helper.index("dian_install_transaction_begin()") : helper.index(
                "dian_install_transaction_mark()"
            )
        ]
        self.assertLess(begin.index("dian_durable_sync"), begin.index('/bin/mv "$stage"'))
        self.assertGreater(begin.rindex("dian_durable_sync"), begin.index('/bin/mv "$stage"'))

        recover = helper[
            helper.index("dian_install_transaction_recover()") : helper.index(
                "dian_install_transaction_verify_job()"
            )
        ]
        self.assertIn("rollback_ready", helper)
        self.assertIn("dian_install_recovery_lease_acquire", recover)
        self.assertIn("dian_install_recovery_lease_release", recover)
        self.assertLess(
            recover.index("dian_install_recovery_lease_acquire"),
            recover.index("dian_install_lock_release"),
        )
        self.assertLess(
            recover.index("dian_install_lock_release"),
            recover.index("dian_install_lock_acquire"),
        )
        self.assertLess(
            recover.index("dian_install_transaction_verify_job"),
            recover.index("dian_install_transaction_discard_journal"),
        )

        self.assertIn(".install-recovery.lock", helper)
        self.assertIn("dian_wait_for_recovery_lease", helper)
        self.assertIn("dian_lock_receipt_is_live", helper)
        self.assertIn('[[ "$owner_tail" == *:* ]] || return 1', helper)
        self.assertNotIn('[[ "$owner_tail" == *:* ]] || return 0', helper)
        lease_acquire = helper[
            helper.index("dian_install_recovery_lease_acquire()") : helper.index(
                "dian_install_recovery_lease_release()"
            )
        ]
        self.assertIn("dian_process_fingerprint", lease_acquire)
        self.assertIn("$$:$token:$fingerprint", lease_acquire)
        primary_acquire = helper[
            helper.index("dian_install_lock_acquire()") : helper.index(
                "dian_install_recovery_lease_acquire()"
            )
        ]
        self.assertIn('dian_wait_for_recovery_lease "$root_abs" "$deadline"', primary_acquire)
        self.assertIn("dian_install_cleanup_orphan_stages()", helper)
        cleanup = helper[
            helper.index("dian_install_cleanup_orphan_stages()") : helper.index(
                "dian_install_transaction_cleanup_tombstone()"
            )
        ]
        self.assertIn("directory_candidates", cleanup)
        self.assertIn("file_candidates", cleanup)
        self.assertLess(cleanup.index('[[ -d "$candidate"'), cleanup.index('/bin/rm -rf "$candidate"'))
        self.assertLess(cleanup.index('[[ -f "$candidate"'), cleanup.index('/bin/rm -f "$candidate"'))
        self.assertIn('/bin/mkdir -m 700 "$lock_dir"', helper)
        self.assertIn('/bin/mkdir -m 700 "$lease_dir"', helper)

        self.assertIn('TRANSACTION_DIR="$INSTALL_ROOT/.install-transaction"', launcher)
        self.assertIn('! -L "$TRANSACTION_DIR"', launcher)
        for phase in ("launching", "healthy", "verified", "rollback_ready"):
            self.assertIn(phase, launcher)
        self.assertIn("AUTHORIZED_VERSION", launcher)
        self.assertIn("OLD_VERSION", launcher)
        self.assertIn("LOCK_IS_LIVE", launcher)
        self.assertIn("LOCK_OWNER_PID_ALIVE", launcher)
        self.assertIn('[[ "$receipt_tail" == *:* ]] || return 1', launcher)
        self.assertNotIn('[[ "$receipt_tail" == *:* ]] || return 0', launcher)
        self.assertIn("TRANSACTION_AUTHORIZED", launcher)
        self.assertLess(launcher.index("TRANSACTION_AUTHORIZED"), launcher.index('exec "$AGENT"'))

        for installer in installers:
            self.assertIn('PLIST_TEMP="$(/usr/bin/mktemp ', installer)
            self.assertIn('VERSION_TEMP="$(/usr/bin/mktemp ', installer)
            self.assertNotIn('PLIST_TEMP="$PLIST_PATH.tmp.$$"', installer)
            self.assertNotIn('VERSION_TEMP="$INSTALL_ROOT/.current-version.tmp.$$"', installer)
            version_temp = installer[
                installer.index('VERSION_TEMP="$(/usr/bin/mktemp ') : installer.index(
                    'dian_install_transaction_mark "$INSTALL_ROOT" pointer committing'
                )
            ]
            self.assertIn('${VERSION_TEMP:h:A}', version_temp)
            self.assertIn('-f "$VERSION_TEMP"', version_temp)
            self.assertIn('! -L "$VERSION_TEMP"', version_temp)
            plist_temp = installer[
                installer.index('PLIST_TEMP="$(/usr/bin/mktemp ') : installer.index(
                    '/bin/cat > "$PLIST_TEMP"'
                )
            ]
            self.assertIn('${PLIST_TEMP:h:A}', plist_temp)
            self.assertIn('-f "$PLIST_TEMP"', plist_temp)
            self.assertIn('! -L "$PLIST_TEMP"', plist_temp)
            self.assertIn(
                'dian_install_cleanup_orphan_stages "$INSTALL_ROOT" "$PLIST_PATH"',
                installer,
            )
            self.assertGreaterEqual(installer.count("dian_durable_sync || fail"), 5)
            for switch, committed_mark in (
                (
                    'dian_atomic_directory_commit "$INSTALL_ROOT"',
                    'dian_install_transaction_mark "$INSTALL_ROOT" app committed',
                ),
                (
                    'dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/tools/macos"',
                    'dian_install_transaction_mark "$INSTALL_ROOT" tools committed',
                ),
                (
                    'dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/extension"',
                    'dian_install_transaction_mark "$INSTALL_ROOT" extension committed',
                ),
            ):
                switch_position = installer.index(switch)
                committed_position = installer.index(committed_mark, switch_position)
                sync_position = installer.index(
                    "dian_durable_sync || fail", switch_position
                )
                self.assertLess(sync_position, committed_position)
            pointer_move = installer.index(
                '/bin/mv -f "$VERSION_TEMP" "$INSTALL_ROOT/current-version.txt"'
            )
            pointer_mark = installer.index(
                'dian_install_transaction_mark "$INSTALL_ROOT" pointer committed'
            )
            pointer_sync = installer.index("dian_durable_sync || fail", pointer_move)
            self.assertLess(pointer_move, pointer_sync)
            self.assertLess(pointer_sync, pointer_mark)
            plist_move = installer.index('/bin/mv -f "$PLIST_TEMP" "$PLIST_PATH"')
            plist_mark = installer.index(
                'dian_install_transaction_mark "$INSTALL_ROOT" plist committed'
            )
            plist_sync = installer.index("dian_durable_sync || fail", plist_move)
            self.assertLess(plist_move, plist_sync)
            self.assertLess(plist_sync, plist_mark)

        repair_bootstrap = repair.index("launchctl bootstrap")
        repair_api_proof = repair.index("dian_verify_local_api", repair_bootstrap)
        repair_job_proof = repair.index(
            "dian_install_transaction_verify_job", repair_api_proof
        )
        repair_release = repair.index("dian_install_lock_release || fail", repair_job_proof)
        self.assertLess(repair_bootstrap, repair_api_proof)
        self.assertLess(repair_api_proof, repair_job_proof)
        self.assertLess(repair_job_proof, repair_release)
        repair_flag = repair.index("typeset -g REPAIR_JOB_STARTED=1")
        self.assertLess(repair_flag, repair.index("launchctl bootstrap", repair_flag))
        cleanup = repair[repair.index("repair_cleanup()") : repair.index("trap 'repair_cleanup' EXIT")]
        self.assertIn("launchctl bootout", cleanup)
        self.assertIn("launchctl print", cleanup)
        self.assertIn(".repair-pending", repair)
        self.assertLess(repair_job_proof, repair.index('/bin/rm -f "$REPAIR_PENDING"'))
        self.assertIn("--copies", installers[1])
        self.assertIn('! -L "$STAGE/venv/bin/python"', installers[1])

    def test_macos_native_build_self_test_never_uses_real_user_data_or_logs(self) -> None:
        builder = (MACOS_TOOLS / "build_agent.sh").read_text(encoding="utf-8")
        self.assertIn('SELF_TEST_ROOT="$(mktemp -d)"', builder)
        self.assertIn('DIAN_AGENT_INSTALL_ROOT="$SELF_TEST_ROOT/install"', builder)
        self.assertIn('DIAN_AGENT_DATA_DIR="$SELF_TEST_ROOT/data"', builder)
        self.assertIn('DIAN_AGENT_LOG_DIR="$SELF_TEST_ROOT/logs"', builder)
        self.assertIn("trap '/bin/rm -rf \"$SELF_TEST_ROOT\"' EXIT", builder)
        self.assertIn('AGENT_VERSION="${VERSIONS%%', builder)
        self.assertIn('[[ "$AGENT_VERSION" == "$MANIFEST_VERSION" ]]', builder)

    def test_macos_native_release_builds_only_from_verified_public_source(self) -> None:
        builder = (MACOS_TOOLS / "build_release.sh").read_text(encoding="utf-8")
        prepare = builder.index("prepare_public_source.py")
        source_check = builder.index('check_public_release.py" --source "$PUBLIC_SOURCE"')
        build = builder.index('"$PUBLIC_SOURCE/tools/macos/build_agent.sh"')
        artifact_check = builder.index('check_public_release.py" --artifact "$STAGE"')
        self.assertLess(prepare, source_check)
        self.assertLess(source_check, build)
        self.assertLess(build, artifact_check)
        self.assertIn('"$PUBLIC_SOURCE/dist/agent-macos/DianAgent"', builder)
        self.assertIn('RUNTIME_TOOLS=(', builder)
        self.assertNotIn('rsync -a "$PROJECT_ROOT/tools/macos/"', builder)
        self.assertNotIn('rsync -a "$PUBLIC_SOURCE/tools/macos/"', builder)
        self.assertIn('/usr/bin/unzip -tq "$ZIP"', builder)
        self.assertIn('BUILD_COMPLETE=1', builder)
        self.assertIn("Unsafe symbolic link in release output path", builder)

    def test_macos_source_release_requires_exact_version_and_valid_zip(self) -> None:
        builder = (ROOT / "tools" / "build_macos_source_release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("AGENT_VERSION", builder)
        self.assertIn("does not exactly match", builder)
        self.assertIn("archive.testzip()", builder)
        self.assertIn('foreach ($oldOutput in @($zipPath, "$zipPath.sha256"))', builder)
        self.assertIn("Remove-Item -LiteralPath $oldOutput -Force", builder)
        self.assertIn("$buildComplete = $true", builder)
        self.assertIn("Assert-NoReparsePointPathChain", builder)

    def test_installers_reject_version_mismatch_before_transaction(self) -> None:
        for name in ("install_dian_agent.command", "install_dian_agent_source.command"):
            installer = (MACOS_TOOLS / name).read_text(encoding="utf-8")
            version_proof = installer.index(
                'RECEIPT_VERSION="$(dian_json_string_field "$TRUST_RECEIPT" "agent_version")"'
            )
            transaction = installer.index('dian_install_transaction_begin "$INSTALL_ROOT"')
            self.assertLess(version_proof, transaction)
            self.assertIn('[[ "$RECEIPT_VERSION" == "$VERSION" ]]', installer)
            self.assertIn('dian_tree_has_no_symlinks "$SOURCE_ROOT/', installer)

    def test_launch_agent_never_executes_symlinked_runtime_entries(self) -> None:
        launcher = (MACOS_TOOLS / "launch_agent.sh").read_text(encoding="utf-8")
        self.assertIn('-L "$APP_ROOT"', launcher)
        self.assertIn('-L "$VERSION_ROOT"', launcher)
        self.assertIn('! -L "$AGENT"', launcher)
        self.assertIn('! -L "$SOURCE_PYTHON"', launcher)
        self.assertIn('! -L "$SOURCE_ENTRY"', launcher)

    def test_trust_helper_derives_fixed_extension_id_and_preserves_credentials(self) -> None:
        expected_extension_id = "obpbbgjamjfkambmhidbjnaoiehfndcj"
        self.assertEqual(
            expected_extension_id,
            TRUST_HELPER.chromium_extension_id(ROOT / "extension" / "manifest.json"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary) / "DianAgent"
            self.assertEqual(
                expected_extension_id,
                TRUST_HELPER.initialize(ROOT / "extension" / "manifest.json", install_root),
            )
            auth_path = install_root / "config" / "local_api_auth.json"
            trust_path = install_root / "config" / "trusted_extension_ids.json"
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            secret = str(auth["secret"])
            secret_bytes = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
            self.assertEqual(32, len(secret_bytes))
            self.assertRegex(str(auth["install_id"]), r"^[0-9a-f]{32}$")

            original_auth = auth_path.read_bytes()
            other_extension_id = "a" * 32
            trust_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "extension_ids": [other_extension_id],
                        "managed_by": "administrator",
                    }
                ),
                encoding="utf-8",
            )
            TRUST_HELPER.initialize(ROOT / "extension" / "manifest.json", install_root)
            merged = json.loads(trust_path.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted([other_extension_id, expected_extension_id]), merged["extension_ids"]
            )
            self.assertEqual("administrator", merged["managed_by"])
            self.assertEqual(original_auth, auth_path.read_bytes())
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(auth_path.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(trust_path.stat().st_mode))

    def test_native_agent_entrypoint_provisions_then_exits_without_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary) / "DianAgent Native"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bridge" / "http_receiver.py"),
                    "--initialize-local-api-trust",
                    str(ROOT / "extension" / "manifest.json"),
                    str(install_root),
                ],
                cwd=ROOT / "bridge",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual("obpbbgjamjfkambmhidbjnaoiehfndcj", result["extension_id"])
            self.assertEqual(["obpbbgjamjfkambmhidbjnaoiehfndcj"], result["trusted_extension_ids"])
            self.assertNotIn("secret", completed.stdout)
            self.assertNotIn("access_token", completed.stdout)
            self.assertTrue((install_root / "config" / "local_api_auth.json").is_file())
            self.assertTrue((install_root / "config" / "trusted_extension_ids.json").is_file())

    def test_source_helper_can_emit_public_install_identity_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRUST_HELPER_PATH),
                    "--manifest",
                    str(ROOT / "extension" / "manifest.json"),
                    "--install-root",
                    str(Path(temporary) / "DianAgent Source"),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertTrue(receipt["ok"])
            self.assertRegex(receipt["install_id"], r"^[0-9a-f]{32}$")
            self.assertEqual("obpbbgjamjfkambmhidbjnaoiehfndcj", receipt["extension_id"])
            self.assertNotIn("secret", completed.stdout)
            self.assertNotIn("access_token", completed.stdout)

    def test_native_agent_explicit_repair_receipt_has_backup_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary) / "DianAgent Native"
            initialize = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bridge" / "http_receiver.py"),
                    "--initialize-local-api-trust",
                    str(ROOT / "extension" / "manifest.json"),
                    str(install_root),
                ],
                cwd=ROOT / "bridge",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(0, initialize.returncode, initialize.stderr)
            initial = json.loads(initialize.stdout)
            trust_path = install_root / "config" / "trusted_extension_ids.json"
            damaged = b'{"schema_version": 999, "extension_ids": []}'
            trust_path.write_bytes(damaged)

            repair = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bridge" / "http_receiver.py"),
                    "--repair-local-api-trust",
                    str(ROOT / "extension" / "manifest.json"),
                    str(install_root),
                ],
                cwd=ROOT / "bridge",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(0, repair.returncode, repair.stderr)
            receipt = json.loads(repair.stdout)
            self.assertTrue(receipt["ok"])
            self.assertTrue(receipt["repaired"])
            self.assertTrue(receipt["rotated"])
            self.assertNotEqual(initial["install_id"], receipt["install_id"])
            self.assertEqual(
                ["obpbbgjamjfkambmhidbjnaoiehfndcj"], receipt["trusted_extension_ids"]
            )
            backup = Path(receipt["backup_path"])
            self.assertEqual(install_root / "config" / "repair-backup", backup.parent)
            self.assertEqual(damaged, (backup / "trusted_extension_ids.json").read_bytes())
            self.assertNotIn("secret", repair.stdout)
            self.assertNotIn("access_token", repair.stdout)

            second = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bridge" / "http_receiver.py"),
                    "--repair-local-api-trust",
                    str(ROOT / "extension" / "manifest.json"),
                    str(install_root),
                ],
                cwd=ROOT / "bridge",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(0, second.returncode, second.stderr)
            second_receipt = json.loads(second.stdout)
            self.assertFalse(second_receipt["repaired"])
            self.assertFalse(second_receipt["rotated"])
            self.assertIsNone(second_receipt["backup_path"])
            self.assertEqual(receipt["install_id"], second_receipt["install_id"])

    def test_trust_helper_uses_atomic_writes_and_corruption_fails_closed(self) -> None:
        auth_source = (ROOT / "bridge" / "local_api_auth.py").read_text(encoding="utf-8")
        self.assertIn("tempfile.mkstemp", auth_source)
        self.assertIn("os.replace", auth_source)
        self.assertIn("os.fsync", auth_source)
        self.assertIn("os.chmod(temporary, 0o600)", auth_source)
        self.assertIn("os.chmod(path, 0o600)", auth_source)

        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary) / "DianAgent"
            config = install_root / "config"
            config.mkdir(parents=True)
            trust_path = config / "trusted_extension_ids.json"
            auth_path = config / "local_api_auth.json"
            damaged = '{"schema_version": 1, "extension_ids": ["not-an-id"]}'
            trust_path.write_text(damaged, encoding="utf-8")
            with self.assertRaises(TRUST_HELPER.ProvisioningError):
                TRUST_HELPER.initialize(ROOT / "extension" / "manifest.json", install_root)
            self.assertEqual(damaged, trust_path.read_text(encoding="utf-8"))
            self.assertFalse(auth_path.exists())

        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary) / "DianAgent"
            config = install_root / "config"
            config.mkdir(parents=True)
            auth_path = config / "local_api_auth.json"
            trust_path = config / "trusted_extension_ids.json"
            damaged = '{"schema_version": 1, "install_id": "broken", "secret": "broken"}'
            auth_path.write_text(damaged, encoding="utf-8")
            with self.assertRaises(TRUST_HELPER.ProvisioningError):
                TRUST_HELPER.initialize(ROOT / "extension" / "manifest.json", install_root)
            self.assertEqual(damaged, auth_path.read_text(encoding="utf-8"))
            self.assertFalse(trust_path.exists())

    def test_launch_agent_supports_native_and_isolated_source_runtimes(self) -> None:
        launcher = (MACOS_TOOLS / "launch_agent.sh").read_text(encoding="utf-8")
        self.assertIn('app/$VERSION/DianAgent', launcher)
        self.assertIn('app/$VERSION/venv/bin/python', launcher)
        self.assertIn("DIAN_AGENT_AUTOSTART_SOURCE", launcher)
        self.assertIn('exec "$AGENT"', launcher)

        repair = (MACOS_TOOLS / "repair_dian_agent.command").read_text(encoding="utf-8")
        verifier = (MACOS_TOOLS / "verify_local_api.sh").read_text(encoding="utf-8")
        self.assertIn("current-version.txt", repair)
        repair_command = repair.index("--repair-local-api-trust")
        self.assertLess(repair_command, repair.index("launchctl bootstrap"))
        self.assertIn('source "$VERIFY_HELPER"', repair)
        self.assertIn('dian_verify_local_api "$TRUST_RECEIPT" "$VERSION" 40', repair)
        self.assertIn("/auth/session", verifier)
        self.assertIn("/auth/status", verifier)
        self.assertIn("Origin: chrome-extension://$extension_id", verifier)
        self.assertIn("X-Dian-Agent-Extension-Id: $extension_id", verifier)
        self.assertIn("X-Dian-Agent-Extension-Version: $expected_version", verifier)
        self.assertIn('\\"extension_version\\":\\"$expected_version\\"', verifier)
        self.assertIn("X-Dian-Agent-Token: $token", verifier)
        self.assertIn("/usr/bin/curl --config -", verifier)
        self.assertIn('expected_install_id="$(dian_json_string_field "$trust_receipt" "install_id")"', verifier)
        self.assertIn('receipt_agent_version="$(dian_json_string_field "$trust_receipt" "agent_version")"', verifier)
        self.assertIn('[[ "$receipt_agent_version" != "$expected_version" ]]', verifier)
        self.assertIn('session_install_id="$(dian_json_string_field "$auth_response" "install_id")"', verifier)
        self.assertIn('[[ "$session_install_id" != "$expected_install_id" ]]', verifier)
        self.assertIn("session_extension_version", verifier)
        self.assertIn("authenticated", verifier)
        self.assertIn('token=""', verifier)
        self.assertNotIn('print -r -- "$TRUST_RECEIPT"', repair)

    def test_both_macos_release_formats_ship_the_authenticated_repair_entry(self) -> None:
        native_builder = (MACOS_TOOLS / "build_release.sh").read_text(encoding="utf-8")
        source_builder = (ROOT / "tools" / "build_macos_source_release.ps1").read_text(
            encoding="utf-8"
        )
        repair = (MACOS_TOOLS / "repair_dian_agent.command").read_text(encoding="utf-8")

        self.assertIn('repair_dian_agent.command" "$STAGE/repair_dian_agent.command', native_builder)
        self.assertIn('"repair_dian_agent.command"', source_builder)
        self.assertIn(
            'Copy-Item -LiteralPath (Join-Path $publicSource "tools\\macos\\repair_dian_agent.command")',
            source_builder,
        )
        self.assertIn('app/$VERSION/DianAgent', repair)
        self.assertIn('app/$VERSION/venv/bin/python', repair)
        self.assertIn('bridge/http_receiver.py', repair)
        self.assertIn('"verify_local_api.sh"', source_builder)
        verifier = (MACOS_TOOLS / "verify_local_api.sh").read_text(encoding="utf-8")
        self.assertIn("dian_verify_local_api", verifier)

    def test_oauth_uses_native_keychain_api_without_secret_cli_arguments(self) -> None:
        source = (ROOT / "bridge" / "oceanengine_oauth.py").read_text(encoding="utf-8")
        self.assertIn("SecKeychainAddGenericPassword", source)
        self.assertIn("SecKeychainFindGenericPassword", source)
        self.assertNotIn("add-generic-password", source)
        self.assertNotIn("security\"", source)

    def test_macos_release_is_manual_and_fails_fast_outside_apple_silicon(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-macos.yml").read_text(
            encoding="utf-8"
        )
        native_builder = (MACOS_TOOLS / "build_agent.sh").read_text(encoding="utf-8")
        release_builder = (MACOS_TOOLS / "build_release.sh").read_text(encoding="utf-8")
        native_installer = (MACOS_TOOLS / "install_dian_agent.command").read_text(
            encoding="utf-8"
        )
        source_installer = (MACOS_TOOLS / "install_dian_agent_source.command").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_dispatch", workflow)
        self.assertIn('[[ "$ARCH" == "arm64" ]]', native_builder)
        self.assertIn('[[ "$ARCH" == "arm64" ]]', release_builder)
        behavior_gate = (
            'test_macos_release.MacOSInstallTransactionBehaviorTests'
        )
        self.assertIn(behavior_gate, release_builder)
        self.assertLess(
            release_builder.index(behavior_gate),
            release_builder.index('"$PUBLIC_SOURCE/tools/macos/build_agent.sh"'),
        )
        self.assertNotIn('"x86_64"', release_builder)
        self.assertIn('[[ "$MACHINE" == "arm64" ]]', native_installer)
        self.assertIn('[[ "$PACKAGE_ARCH" == "arm64" ]]', native_installer)
        self.assertIn('[[ "$MACHINE" == "arm64" ]]', source_installer)
        self.assertIn('[[ "$PYTHON_MACHINE" == "arm64" ]]', source_installer)


@unittest.skipUnless(sys.platform == "darwin", "requires macOS zsh and launchctl")
class MacOSInstallTransactionBehaviorTests(unittest.TestCase):
    VERSION = "1.0.0"
    OLD_VERSION = "0.9.0"

    @staticmethod
    def _plist(label: str, marker: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'<key>Label</key><string>{label}</string>\n'
            '<key>ProgramArguments</key><array><string>/usr/bin/true</string></array>\n'
            f'<key>TransactionTestMarker</key><string>{marker}</string>\n'
            '</dict></plist>\n'
        ).encode("utf-8")

    @staticmethod
    def _running_plist(label: str, marker: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'<key>Label</key><string>{label}</string>\n'
            '<key>ProgramArguments</key><array>'
            '<string>/bin/sleep</string><string>300</string></array>\n'
            '<key>RunAtLoad</key><true/>\n'
            f'<key>TransactionTestMarker</key><string>{marker}</string>\n'
            '</dict></plist>\n'
        ).encode("utf-8")

    @staticmethod
    def _write_executable(path: Path, marker: str) -> None:
        path.write_text(f"#!/bin/sh\n# {marker}\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    def _seed_existing_install(self, install_root: Path) -> None:
        old_app = install_root / "app" / self.OLD_VERSION
        old_app.mkdir(parents=True)
        self._write_executable(old_app / "DianAgent", "old-app")
        (old_app / "sentinel.txt").write_bytes(b"old-app-sentinel\x00\xff")
        same_version_app = install_root / "app" / self.VERSION
        same_version_app.mkdir(parents=True)
        self._write_executable(same_version_app / "DianAgent", "old-same-version-app")
        (same_version_app / "sentinel.txt").write_bytes(
            b"old-same-version-app-sentinel\x00\xfc"
        )

        old_tools = install_root / "tools" / "macos"
        old_tools.mkdir(parents=True)
        for name in (
            "launch_agent.sh",
            "verify_local_api.sh",
            "atomic_directory_update.sh",
            "repair_dian_agent.command",
        ):
            tool = old_tools / name
            tool.write_text(f"old-tool:{name}\n", encoding="utf-8")
            tool.chmod(0o755)
        (old_tools / "sentinel.txt").write_bytes(b"old-tools-sentinel\x00\xfe")

        old_extension = install_root / "extension-current"
        old_extension.mkdir(parents=True)
        (old_extension / "manifest.json").write_text(
            '{"name":"old-extension","version":"0.9.0"}\n', encoding="utf-8"
        )
        (old_extension / "sentinel.txt").write_bytes(b"old-extension-sentinel\x00\xfd")
        (install_root / "current-version.txt").write_bytes(b"0.9.0\n")

    def _environment(
        self,
        home: Path,
        install_root: Path,
        plist_path: Path,
        new_plist_path: Path,
        label: str,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "TEST_INSTALL_ROOT": str(install_root),
                "TEST_PLIST": str(plist_path),
                "TEST_NEW_PLIST": str(new_plist_path),
                "TEST_HELPER": str(MACOS_TOOLS / "atomic_directory_update.sh"),
                "TEST_LABEL": label,
                "TEST_VERSION": self.VERSION,
                "LC_ALL": "C",
            }
        )
        return environment

    def _run_zsh(
        self, script: str, environment: dict[str, str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/zsh", "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def _crash_after_full_switch_script() -> str:
        return r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
PLIST="$TEST_PLIST"
DOMAIN="gui/$UID"

dian_install_lock_acquire "$ROOT" 10
dian_install_transaction_begin "$ROOT" "$TEST_VERSION" "$PLIST" "$DOMAIN" "$TEST_LABEL"
dian_install_transaction_phase "$ROOT" switching

/bin/mkdir -p "$ROOT/app" "$ROOT/tools"
APP_STAGE="$ROOT/app/.transaction-app-stage.$$"
/bin/mkdir "$APP_STAGE"
print -r -- '#!/bin/sh' 'exit 0' > "$APP_STAGE/DianAgent"
/bin/chmod 755 "$APP_STAGE/DianAgent"
print -rn -- 'new-app-sentinel' > "$APP_STAGE/sentinel.txt"
dian_install_transaction_mark "$ROOT" app committing
dian_atomic_directory_commit "$ROOT" "$APP_STAGE" "$ROOT/app/$TEST_VERSION" dian_validate_any_runtime_tree
dian_durable_sync
dian_install_transaction_mark "$ROOT" app committed

TOOLS_STAGE="$ROOT/tools/.transaction-tools-stage.$$"
/bin/mkdir "$TOOLS_STAGE"
for name in launch_agent.sh verify_local_api.sh atomic_directory_update.sh repair_dian_agent.command; do
  print -r -- "new-tool:$name" > "$TOOLS_STAGE/$name"
  /bin/chmod 755 "$TOOLS_STAGE/$name"
done
print -rn -- 'new-tools-sentinel' > "$TOOLS_STAGE/sentinel.txt"
dian_install_transaction_mark "$ROOT" tools committing
dian_atomic_directory_commit "$ROOT" "$TOOLS_STAGE" "$ROOT/tools/macos" dian_validate_installed_tools_tree
dian_durable_sync
dian_install_transaction_mark "$ROOT" tools committed

EXTENSION_STAGE="$ROOT/.transaction-extension-stage.$$"
/bin/mkdir "$EXTENSION_STAGE"
print -r -- '{"name":"new-extension","version":"1.0.0"}' > "$EXTENSION_STAGE/manifest.json"
print -rn -- 'new-extension-sentinel' > "$EXTENSION_STAGE/sentinel.txt"
dian_install_transaction_mark "$ROOT" extension committing
dian_atomic_directory_commit "$ROOT" "$EXTENSION_STAGE" "$ROOT/extension-current" dian_validate_installed_extension_tree
dian_durable_sync
dian_install_transaction_mark "$ROOT" extension committed

dian_install_transaction_mark "$ROOT" pointer committing
POINTER_TEMP="$(/usr/bin/mktemp "$ROOT/.transaction-pointer.XXXXXX")"
print -r -- "$TEST_VERSION" > "$POINTER_TEMP"
/bin/mv -f "$POINTER_TEMP" "$ROOT/current-version.txt"
dian_durable_sync
dian_install_transaction_mark "$ROOT" pointer committed

dian_install_transaction_mark "$ROOT" plist committing
PLIST_TEMP="$(/usr/bin/mktemp "${PLIST:h}/.transaction-plist.XXXXXX")"
/bin/cp -p "$TEST_NEW_PLIST" "$PLIST_TEMP"
/bin/mv -f "$PLIST_TEMP" "$PLIST"
dian_durable_sync
dian_install_transaction_mark "$ROOT" plist committed

print -r -- ready > "$ROOT/crash-ready"
dian_durable_sync
/bin/kill -9 "$$"
'''

    @classmethod
    def _fault_matrix_script(cls) -> str:
        script = cls._crash_after_full_switch_script()
        script = script.replace(
            'DOMAIN="gui/$UID"\n',
            '''DOMAIN="gui/$UID"

crash_if() {
  [[ "${TEST_FAULT_POINT:-}" == "$1" ]] || return 0
  print -r -- "$1" > "$ROOT/crash-ready"
  dian_durable_sync
  /bin/kill -9 "$$"
}
''',
            1,
        )
        for component, point in (
            ("app", "after-app"),
            ("tools", "after-tools"),
            ("extension", "after-extension"),
            ("pointer", "after-pointer"),
            ("plist", "after-plist"),
        ):
            receipt = f'dian_install_transaction_mark "$ROOT" {component} committed'
            script = script.replace(receipt, f"{receipt}\ncrash_if {point}", 1)
        old_tail = '''print -r -- ready > "$ROOT/crash-ready"
dian_durable_sync
/bin/kill -9 "$$"'''
        launch_tail = '''dian_install_transaction_phase "$ROOT" launching
dian_install_transaction_authorize_launch "$ROOT" "$TEST_VERSION"
/bin/launchctl bootout "$DOMAIN/$TEST_LABEL" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "$DOMAIN" "$PLIST"
crash_if after-bootstrap
/bin/launchctl kickstart -k "$DOMAIN/$TEST_LABEL"
crash_if after-kickstart
ATTEMPT=0
while ! /bin/launchctl print "$DOMAIN/$TEST_LABEL" 2>/dev/null | /usr/bin/grep -q 'state = running'; do
  (( ATTEMPT += 1 ))
  (( ATTEMPT < 50 )) || exit 93
  /bin/sleep 0.1
done
dian_install_transaction_phase "$ROOT" healthy
crash_if after-health
exit 94'''
        if old_tail not in script:
            raise AssertionError("fault-matrix template tail changed")
        return script.replace(old_tail, launch_tail, 1)

    @staticmethod
    def _recover_script() -> str:
        return r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
DOMAIN="gui/$UID"
dian_install_lock_acquire "$ROOT" 10
trap 'dian_install_lock_release' EXIT
dian_install_transaction_recover "$ROOT" "$TEST_PLIST" "$DOMAIN" "$TEST_LABEL"
'''

    def test_power_loss_restores_every_existing_tree_and_exact_pointer_and_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            self._seed_existing_install(install_root)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            old_plist = self._plist("com.dianagent.agent", "old-plist")
            new_plist = self._plist("com.dianagent.agent", "new-plist")
            plist_path.write_bytes(old_plist)
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(new_plist)
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.existing",
            )

            crashed = self._run_zsh(self._crash_after_full_switch_script(), environment)
            self.assertNotEqual(0, crashed.returncode, crashed.stderr)
            self.assertTrue((install_root / "crash-ready").is_file(), crashed.stderr)
            self.assertTrue((install_root / ".install-transaction").is_dir())

            recovered = self._run_zsh(self._recover_script(), environment)
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertEqual(b"0.9.0\n", (install_root / "current-version.txt").read_bytes())
            self.assertEqual(old_plist, plist_path.read_bytes())
            self.assertEqual(
                b"old-app-sentinel\x00\xff",
                (install_root / "app" / self.OLD_VERSION / "sentinel.txt").read_bytes(),
            )
            self.assertEqual(
                b"old-same-version-app-sentinel\x00\xfc",
                (install_root / "app" / self.VERSION / "sentinel.txt").read_bytes(),
            )
            self.assertEqual(
                b"old-tools-sentinel\x00\xfe",
                (install_root / "tools" / "macos" / "sentinel.txt").read_bytes(),
            )
            self.assertEqual(
                b"old-extension-sentinel\x00\xfd",
                (install_root / "extension-current" / "sentinel.txt").read_bytes(),
            )
            self.assertFalse((install_root / ".install-transaction").exists())
            self.assertFalse((install_root / ".install-operation.lock").exists())
            self.assertFalse((install_root / ".install-recovery.lock").exists())
            self.assertFalse((install_root / ".extension-current.previous").exists())
            self.assertFalse((install_root / "tools" / ".macos.previous").exists())
            self.assertFalse((install_root / "app" / f".{self.VERSION}.previous").exists())

    def test_every_installer_fault_point_restores_old_state_and_unloads_new_job(self) -> None:
        fault_points = (
            "after-app",
            "after-tools",
            "after-extension",
            "after-pointer",
            "after-plist",
            "after-bootstrap",
            "after-kickstart",
            "after-health",
        )
        domain = f"gui/{os.getuid()}"
        for index, fault_point in enumerate(fault_points):
            with self.subTest(fault_point=fault_point), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                install_root = home / "Library" / "Application Support" / "DianAgent"
                launch_agents = home / "Library" / "LaunchAgents"
                launch_agents.mkdir(parents=True)
                self._seed_existing_install(install_root)
                label = (
                    f"com.dianagent.transaction-test.{os.getpid()}."
                    f"fault-{index}"
                )
                plist_path = launch_agents / "com.dianagent.agent.plist"
                old_plist = self._plist(label, "old-plist")
                plist_path.write_bytes(old_plist)
                new_plist_path = Path(temporary) / "new.plist"
                new_plist_path.write_bytes(self._running_plist(label, "new-plist"))
                environment = self._environment(
                    home,
                    install_root,
                    plist_path,
                    new_plist_path,
                    label,
                )
                environment["TEST_FAULT_POINT"] = fault_point
                try:
                    crashed = self._run_zsh(
                        self._fault_matrix_script(), environment, timeout=45
                    )
                    self.assertNotEqual(0, crashed.returncode, crashed.stderr)
                    self.assertEqual(
                        fault_point,
                        (install_root / "crash-ready").read_text(encoding="utf-8").strip(),
                    )
                    self.assertTrue((install_root / ".install-transaction").is_dir())

                    recovered = self._run_zsh(
                        self._recover_script(), environment, timeout=45
                    )
                    self.assertEqual(0, recovered.returncode, recovered.stderr)
                    self.assertEqual(
                        b"0.9.0\n",
                        (install_root / "current-version.txt").read_bytes(),
                    )
                    self.assertEqual(old_plist, plist_path.read_bytes())
                    self.assertEqual(
                        b"old-same-version-app-sentinel\x00\xfc",
                        (install_root / "app" / self.VERSION / "sentinel.txt").read_bytes(),
                    )
                    self.assertEqual(
                        b"old-tools-sentinel\x00\xfe",
                        (install_root / "tools" / "macos" / "sentinel.txt").read_bytes(),
                    )
                    self.assertEqual(
                        b"old-extension-sentinel\x00\xfd",
                        (install_root / "extension-current" / "sentinel.txt").read_bytes(),
                    )
                    self.assertFalse((install_root / ".install-transaction").exists())
                    self.assertFalse((install_root / ".install-operation.lock").exists())
                    self.assertFalse((install_root / ".install-recovery.lock").exists())
                    job = subprocess.run(
                        ["/bin/launchctl", "print", f"{domain}/{label}"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertNotEqual(0, job.returncode, job.stdout)
                finally:
                    subprocess.run(
                        ["/bin/launchctl", "bootout", f"{domain}/{label}"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )

    def test_power_loss_during_fresh_install_removes_every_new_active_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "fresh"))
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.fresh",
            )

            crashed = self._run_zsh(self._crash_after_full_switch_script(), environment)
            self.assertNotEqual(0, crashed.returncode, crashed.stderr)
            self.assertTrue((install_root / "crash-ready").is_file(), crashed.stderr)
            recovered = self._run_zsh(self._recover_script(), environment)
            self.assertEqual(0, recovered.returncode, recovered.stderr)

            self.assertFalse((install_root / "app" / self.VERSION).exists())
            self.assertFalse((install_root / "tools" / "macos").exists())
            self.assertFalse((install_root / "extension-current").exists())
            self.assertFalse((install_root / "current-version.txt").exists())
            self.assertFalse(plist_path.exists())
            self.assertFalse((install_root / ".install-transaction").exists())
            self.assertFalse((install_root / ".install-operation.lock").exists())
            self.assertFalse((install_root / ".install-recovery.lock").exists())

    def test_verified_finalize_cleanup_is_idempotent_after_partial_backup_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            self._seed_existing_install(install_root)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            plist_path.write_bytes(self._plist("com.dianagent.agent", "old"))
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "new"))
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.verified-cleanup",
            )
            script = self._crash_after_full_switch_script().replace(
                'print -r -- ready > "$ROOT/crash-ready"\ndian_durable_sync\n/bin/kill -9 "$$"',
                'dian_install_transaction_authorize_launch "$ROOT" "$TEST_VERSION"\n'
                'dian_install_transaction_phase "$ROOT" verified\n'
                '/bin/rm -rf "$ROOT/.extension-current.previous"\n'
                'print -r -- ready > "$ROOT/crash-ready"\n'
                'dian_durable_sync\n/bin/kill -9 "$$"',
            )
            crashed = self._run_zsh(script, environment)
            self.assertNotEqual(0, crashed.returncode, crashed.stderr)
            recovered = self._run_zsh(self._recover_script(), environment)
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertEqual(b"1.0.0\n", (install_root / "current-version.txt").read_bytes())
            self.assertEqual(
                b"new-app-sentinel",
                (install_root / "app" / self.VERSION / "sentinel.txt").read_bytes(),
            )
            self.assertFalse((install_root / ".install-transaction").exists())
            self.assertFalse((install_root / "tools" / ".macos.previous").exists())
            self.assertFalse((install_root / "app" / f".{self.VERSION}.previous").exists())

    def test_dead_lock_owner_with_pending_journal_never_launches_pending_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            runtime = install_root / "app" / self.VERSION / "DianAgent"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                "#!/bin/sh\nprintf launched > \"$DIAN_AGENT_INSTALL_ROOT/should-not-launch\"\n",
                encoding="utf-8",
            )
            runtime.chmod(0o755)
            (install_root / "current-version.txt").write_text(
                f"{self.VERSION}\n", encoding="utf-8"
            )
            lock = install_root / ".install-operation.lock"
            lock.mkdir()
            (lock / "owner").write_text("99999999:dead:dead\n", encoding="utf-8")
            journal = install_root / ".install-transaction"
            journal.mkdir()
            (journal / "schema").write_text("1\n", encoding="utf-8")
            (journal / "version").write_text(f"{self.VERSION}\n", encoding="utf-8")
            (journal / "phase").write_text("switching\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "DIAN_AGENT_INSTALL_ROOT": str(install_root),
                    "LC_ALL": "C",
                }
            )
            blocked = subprocess.run(
                ["/bin/zsh", str(MACOS_TOOLS / "launch_agent.sh")],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(75, blocked.returncode, blocked.stderr)
            self.assertFalse((install_root / "should-not-launch").exists())

    def test_live_legacy_receipt_cannot_authorize_pending_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            runtime = install_root / "app" / self.VERSION / "DianAgent"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                "#!/bin/sh\nprintf launched > \"$DIAN_AGENT_INSTALL_ROOT/should-not-launch\"\n",
                encoding="utf-8",
            )
            runtime.chmod(0o755)
            (install_root / "current-version.txt").write_text(
                f"{self.VERSION}\n", encoding="utf-8"
            )
            legacy_owner = f"{os.getpid()}:legacy-token"
            lock = install_root / ".install-operation.lock"
            lock.mkdir()
            (lock / "owner").write_text(f"{legacy_owner}\n", encoding="utf-8")
            journal = install_root / ".install-transaction"
            journal.mkdir()
            fields = {
                "schema": "1",
                "version": self.VERSION,
                "phase": "launching",
                "launch_owner": legacy_owner,
                "authorized_version": self.VERSION,
                "app_state": "committed",
                "tools_state": "committed",
                "extension_state": "committed",
                "pointer_state": "committed",
                "plist_state": "committed",
            }
            for name, value in fields.items():
                (journal / name).write_text(f"{value}\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "DIAN_AGENT_INSTALL_ROOT": str(install_root),
                    "LC_ALL": "C",
                }
            )
            blocked = subprocess.run(
                ["/bin/zsh", str(MACOS_TOOLS / "launch_agent.sh")],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(75, blocked.returncode, blocked.stderr)
            self.assertFalse((install_root / "should-not-launch").exists())

    def test_corrupt_rollback_backup_fails_closed_and_retains_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            self._seed_existing_install(install_root)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            plist_path.write_bytes(self._plist("com.dianagent.agent", "old"))
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "new"))
            outside = Path(temporary) / "outside"
            outside.mkdir()
            outside_sentinel = outside / "must-not-change"
            outside_sentinel.write_bytes(b"outside-safe")
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.failed-rollback",
            )
            environment["TEST_OUTSIDE"] = str(outside)
            crash_script = r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
DOMAIN="gui/$UID"
dian_install_lock_acquire "$ROOT" 10
dian_install_transaction_begin "$ROOT" "$TEST_VERSION" "$TEST_PLIST" "$DOMAIN" "$TEST_LABEL"
dian_install_transaction_phase "$ROOT" switching
EXTENSION_STAGE="$ROOT/.transaction-extension-stage.$$"
/bin/mkdir "$EXTENSION_STAGE"
print -r -- '{"name":"new-extension","version":"1.0.0"}' > "$EXTENSION_STAGE/manifest.json"
dian_install_transaction_mark "$ROOT" extension committing
dian_atomic_directory_commit "$ROOT" "$EXTENSION_STAGE" "$ROOT/extension-current" dian_validate_installed_extension_tree
dian_install_transaction_mark "$ROOT" extension committed
/bin/rm -rf "$ROOT/.extension-current.previous"
/bin/ln -s "$TEST_OUTSIDE" "$ROOT/.extension-current.previous"
print -r -- ready > "$ROOT/crash-ready"
dian_durable_sync
/bin/kill -9 "$$"
'''
            crashed = self._run_zsh(crash_script, environment)
            self.assertNotEqual(0, crashed.returncode, crashed.stderr)
            self.assertTrue((install_root / "crash-ready").is_file(), crashed.stderr)

            recovered = self._run_zsh(self._recover_script(), environment)
            self.assertNotEqual(0, recovered.returncode)
            self.assertTrue((install_root / ".install-transaction").is_dir())
            self.assertTrue((install_root / ".extension-current.previous").is_symlink())
            self.assertFalse((install_root / ".install-operation.lock").exists())
            self.assertEqual(b"outside-safe", outside_sentinel.read_bytes())

    def test_transaction_begin_refuses_symlinked_active_tree_without_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            install_root.mkdir(parents=True)
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "new"))
            outside = Path(temporary) / "outside"
            outside.mkdir()
            outside_sentinel = outside / "must-not-change"
            outside_sentinel.write_bytes(b"outside-safe")
            (install_root / "extension-current").symlink_to(outside, target_is_directory=True)
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.symlink",
            )
            refused = self._run_zsh(
                r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
DOMAIN="gui/$UID"
dian_install_lock_acquire "$ROOT" 10
if dian_install_transaction_begin "$ROOT" "$TEST_VERSION" "$TEST_PLIST" "$DOMAIN" "$TEST_LABEL"; then
  dian_install_lock_release
  exit 90
fi
dian_install_lock_release
''',
                environment,
            )
            self.assertEqual(0, refused.returncode, refused.stderr)
            self.assertTrue((install_root / "extension-current").is_symlink())
            self.assertFalse((install_root / ".install-transaction").exists())
            self.assertEqual(b"outside-safe", outside_sentinel.read_bytes())

    def test_orphan_previous_is_restored_before_transaction_baseline_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            previous = install_root / "app" / f".{self.VERSION}.previous"
            previous.mkdir(parents=True)
            self._write_executable(previous / "DianAgent", "orphan-old-app")
            (previous / "sentinel.txt").write_bytes(b"orphan-old-bytes")
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "new"))
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.orphan",
            )
            completed = self._run_zsh(
                r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
DOMAIN="gui/$UID"
dian_install_lock_acquire "$ROOT" 10
dian_install_transaction_begin "$ROOT" "$TEST_VERSION" "$TEST_PLIST" "$DOMAIN" "$TEST_LABEL"
dian_install_transaction_recover "$ROOT" "$TEST_PLIST" "$DOMAIN" "$TEST_LABEL"
dian_install_lock_release
''',
                environment,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            target = install_root / "app" / self.VERSION
            self.assertEqual(b"orphan-old-bytes", (target / "sentinel.txt").read_bytes())
            self.assertFalse(previous.exists())
            self.assertFalse((install_root / ".install-transaction").exists())

    def test_orphan_stage_cleanup_is_bounded_and_symlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            app_root = install_root / "app"
            tools_root = install_root / "tools"
            launch_agents = home / "Library" / "LaunchAgents"
            app_root.mkdir(parents=True)
            tools_root.mkdir()
            launch_agents.mkdir(parents=True)
            directory_candidates = (
                install_root / ".install-transaction.stage.111.222",
                install_root / ".install-operation.stale.111.222",
                install_root / ".install-recovery.stale.111.222",
                install_root / ".extension-current.stage.111",
                app_root / ".stage-1.0.0-111",
                tools_root / ".macos.stage.111",
            )
            for candidate in directory_candidates:
                candidate.mkdir()
            file_candidates = (
                install_root / ".current-version.tmp.abcdef",
                install_root / ".current-version.restore.abcdef",
                install_root / ".repair-pending.tmp.abcdef",
                launch_agents / ".com.dianagent.agent.plist.tmp.abcdef",
                launch_agents / ".com.dianagent.agent.plist.restore.abcdef",
            )
            for candidate in file_candidates:
                candidate.write_text("orphan\n", encoding="utf-8")
            unrelated = install_root / "data" / "must-remain"
            unrelated.parent.mkdir()
            unrelated.write_bytes(b"user-data")
            outside = Path(temporary) / "outside"
            outside.mkdir()
            outside_sentinel = outside / "must-not-change"
            outside_sentinel.write_bytes(b"outside-safe")
            unsafe = app_root / ".stage-unsafe"
            unsafe.symlink_to(outside, target_is_directory=True)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            new_plist_path = Path(temporary) / "unused.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "unused"))
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.cleanup",
            )
            refused = self._run_zsh(
                r'''
set -eu
source "$TEST_HELPER"
dian_install_lock_acquire "$TEST_INSTALL_ROOT" 10
if dian_install_cleanup_orphan_stages "$TEST_INSTALL_ROOT" "$TEST_PLIST"; then
  dian_install_lock_release
  exit 90
fi
dian_install_lock_release
''',
                environment,
            )
            self.assertEqual(0, refused.returncode, refused.stderr)
            self.assertTrue(all(candidate.exists() for candidate in directory_candidates))
            self.assertTrue(all(candidate.exists() for candidate in file_candidates))
            self.assertEqual(b"outside-safe", outside_sentinel.read_bytes())

            unsafe.unlink()
            cleaned = self._run_zsh(
                r'''
set -eu
source "$TEST_HELPER"
dian_install_lock_acquire "$TEST_INSTALL_ROOT" 10
dian_install_cleanup_orphan_stages "$TEST_INSTALL_ROOT" "$TEST_PLIST"
dian_install_lock_release
''',
                environment,
            )
            self.assertEqual(0, cleaned.returncode, cleaned.stderr)
            self.assertTrue(all(not candidate.exists() for candidate in directory_candidates))
            self.assertTrue(all(not candidate.exists() for candidate in file_candidates))
            self.assertEqual(b"user-data", unrelated.read_bytes())
            self.assertEqual(b"outside-safe", outside_sentinel.read_bytes())
            self.assertFalse((install_root / ".install-operation.lock").exists())

    def test_ambiguous_active_and_previous_trees_are_preserved_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            target = install_root / "app" / self.VERSION
            previous = install_root / "app" / f".{self.VERSION}.previous"
            target.mkdir(parents=True)
            previous.mkdir(parents=True)
            self._write_executable(target / "DianAgent", "ambiguous-active")
            self._write_executable(previous / "DianAgent", "ambiguous-previous")
            (target / "sentinel.txt").write_bytes(b"active-bytes")
            (previous / "sentinel.txt").write_bytes(b"previous-bytes")
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist_path = launch_agents / "com.dianagent.agent.plist"
            new_plist_path = Path(temporary) / "new.plist"
            new_plist_path.write_bytes(self._plist("com.dianagent.agent", "new"))
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist_path,
                f"com.dianagent.transaction-test.{os.getpid()}.ambiguous",
            )
            refused = self._run_zsh(
                r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
DOMAIN="gui/$UID"
dian_install_lock_acquire "$ROOT" 10
if dian_install_transaction_begin "$ROOT" "$TEST_VERSION" "$TEST_PLIST" "$DOMAIN" "$TEST_LABEL"; then
  exit 90
fi
dian_install_lock_release
''',
                environment,
            )
            self.assertEqual(0, refused.returncode, refused.stderr)
            self.assertEqual(b"active-bytes", (target / "sentinel.txt").read_bytes())
            self.assertEqual(b"previous-bytes", (previous / "sentinel.txt").read_bytes())
            self.assertFalse((install_root / ".install-transaction").exists())

    def test_installed_stable_bootstrap_recovers_a_missing_tools_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            install_root = home / "Library" / "Application Support" / "DianAgent"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            self._seed_existing_install(install_root)
            launched = install_root / "bootstrap-launched"
            launcher = install_root / "tools" / "macos" / "launch_agent.sh"
            launcher.write_text(
                '#!/bin/sh\nprintf launched > "$DIAN_AGENT_INSTALL_ROOT/bootstrap-launched"\n',
                encoding="utf-8",
            )
            launcher.chmod(0o755)

            plist_path = launch_agents / "com.dianagent.agent.plist"
            original_plist = self._plist("com.dianagent.agent", "original")
            plist_path.write_bytes(original_plist)
            new_plist = launch_agents / "new-stable.plist"
            new_plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '<key>Label</key><string>com.dianagent.agent</string>\n'
                f'<key>ProgramArguments</key><array><string>{install_root}/bootstrap/recovery_bootstrap.sh</string></array>\n'
                '</dict></plist>\n',
                encoding="utf-8",
            )
            environment = self._environment(
                home,
                install_root,
                plist_path,
                new_plist,
                "com.dianagent.agent",
            )
            actor = r'''
set -eu
source "$TEST_HELPER"
ROOT="$TEST_INSTALL_ROOT"
PLIST="$TEST_PLIST"
DOMAIN="gui/$UID"
dian_install_lock_acquire "$ROOT" 10
dian_install_bootstrap_update "$ROOT" "${TEST_HELPER:h}"
STAGED="$(/usr/bin/mktemp "${PLIST:h}/.com.dianagent.agent.plist.tmp.XXXXXX")"
/bin/cp "$TEST_NEW_PLIST" "$STAGED"
dian_install_prepare_stable_plist_migration "$ROOT" "$PLIST" "$STAGED"
dian_install_transaction_begin "$ROOT" "$TEST_VERSION" "$PLIST" "$DOMAIN" "$TEST_LABEL"
dian_install_transaction_phase "$ROOT" switching
dian_install_transaction_mark "$ROOT" plist committing
dian_install_transaction_mark "$ROOT" plist committed
dian_install_transaction_mark "$ROOT" tools committing
/bin/mv "$ROOT/tools/macos" "$ROOT/tools/.macos.previous"
dian_durable_sync
/bin/kill -9 $$
'''
            crashed = self._run_zsh(actor, environment)
            self.assertNotEqual(0, crashed.returncode)
            self.assertFalse((install_root / "tools" / "macos").exists())
            self.assertTrue((install_root / ".install-transaction").is_dir())

            recovered = subprocess.run(
                [str(install_root / "bootstrap" / "recovery_bootstrap.sh")],
                env={
                    **environment,
                    "DIAN_AGENT_INSTALL_ROOT": str(install_root),
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertEqual(original_plist, plist_path.read_bytes())
            self.assertEqual(
                b"old-tools-sentinel\x00\xfe",
                (install_root / "tools" / "macos" / "sentinel.txt").read_bytes(),
            )
            self.assertEqual(b"0.9.0\n", (install_root / "current-version.txt").read_bytes())
            self.assertFalse((install_root / ".install-transaction").exists())
            self.assertFalse(
                (install_root / "bootstrap" / "launchagent.pre-transaction.plist").exists()
            )
            self.assertEqual("launched", launched.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
