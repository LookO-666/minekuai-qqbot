"""No-network tests for destructive install confirmation and durable guards."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import importlib.util
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def state(monkeypatch):
    path = Path(__file__).parents[1] / "plugins" / "minekuai" / "modpack_state.py"
    spec = importlib.util.spec_from_file_location("_test_modpack_state", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server():
    return SimpleNamespace(
        name="test", card_id="test-card", instance_uuid="abcd1234",
        account_phone="private-test-phone", created_at=123,
        token="private-test-token", updated_at=456,
    )


@pytest.fixture
def choice(state):
    return state.InstallChoice(
        project_id="project", item_id="release", name="Test Pack",
        version="1.2", game_version="1.20.1", java_version="17",
        file_name="https://private-download.example.test/token-secret.zip",
        search_query="private-search-query", search_page=2, version_page=3,
    )


@pytest.fixture
def store(state, tmp_path):
    result = state.MaintenanceStore(tmp_path / "data" / "state.db")
    result.init_db()
    return result


def test_identity_ignores_refresh_fields_but_not_persistent_identity(state, server):
    identity = state.ServerIdentity.from_server(server)
    assert identity.matches(server)
    server.token = "refreshed-token"
    server.updated_at += 1
    assert identity.matches(server)
    assert not identity.matches(None)
    assert not identity.matches(SimpleNamespace())


@pytest.mark.parametrize("field,new_value", [
    ("name", "renamed"), ("card_id", "other-card"),
    ("instance_uuid", "other-instance"), ("account_phone", "other-account"),
    ("created_at", 124),
])
def test_identity_change_rejects_previous_selection(state, server, field, new_value):
    identity = state.ServerIdentity.from_server(server)
    setattr(server, field, new_value)
    assert not identity.matches(server)


def test_snapshots_and_choices_are_frozen(state, server, choice):
    identity = state.ServerIdentity.from_server(server)
    pending = state.InstallConfirmStore().issue((1, 2, 3), identity, choice)
    for item, field in [(identity, "name"), (choice, "name"), (pending, "code")]:
        with pytest.raises(FrozenInstanceError):
            setattr(item, field, "changed")


def test_choice_catalog_context_has_backward_compatible_defaults(state):
    choice = state.InstallChoice("project", "item", "pack", "v1", "1.20", "17", "file")
    assert (choice.search_query, choice.search_page, choice.version_page) == ("", 1, 0)


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_confirmation_ttl_must_be_finite_and_positive(state, ttl):
    with pytest.raises(ValueError):
        state.InstallConfirmStore(ttl=ttl)


@pytest.mark.parametrize("other_scope", [
    (9, 2, 3), (1, 9, 3), (1, 2, 9), (1, 2, None),
])
def test_confirmation_cannot_cross_bot_user_or_group(state, server, choice, other_scope):
    confirms = state.InstallConfirmStore()
    pending = confirms.issue((1, 2, 3), state.ServerIdentity.from_server(server), choice)
    with pytest.raises(state.ConfirmError):
        confirms.consume(other_scope, pending.code)
    assert not confirms.cancel(other_scope)
    assert confirms.consume((1, 2, 3), pending.code) == pending


@pytest.mark.parametrize("wrong_code", ["wrong", "", "验证码", None, 123456])
def test_invalid_code_does_not_consume_pending_confirmation(state, server, choice, wrong_code):
    confirms = state.InstallConfirmStore()
    pending = confirms.issue((1, 2, 3), state.ServerIdentity.from_server(server), choice)
    with pytest.raises(state.ConfirmError):
        confirms.consume(pending.scope, wrong_code)
    assert confirms.consume(pending.scope, pending.code) is pending


def test_confirmation_is_single_use_and_cancel_is_scoped(state, server, choice):
    confirms = state.InstallConfirmStore()
    identity = state.ServerIdentity.from_server(server)
    pending = confirms.issue((1, 2, 3), identity, choice)
    assert len(pending.code) == 6 and pending.code.isascii() and pending.code.isdigit()
    assert confirms.consume(pending.scope, pending.code) is pending
    with pytest.raises(state.ConfirmError):
        confirms.consume(pending.scope, pending.code)
    assert not confirms.cancel(pending.scope)
    new = confirms.issue(pending.scope, identity, choice)
    assert confirms.cancel(new.scope)
    with pytest.raises(state.ConfirmError):
        confirms.consume(new.scope, new.code)


@pytest.mark.parametrize("elapsed,valid", [(299.99, True), (300, False), (301, False)])
def test_confirmation_monotonic_ttl_boundary(state, server, choice, elapsed, valid):
    now = [100.0]
    confirms = state.InstallConfirmStore(clock=lambda: now[0], ttl=300)
    pending = confirms.issue((1, 2, 3), state.ServerIdentity.from_server(server), choice)
    now[0] += elapsed
    if valid:
        assert confirms.consume(pending.scope, pending.code) is pending
    else:
        with pytest.raises(state.ConfirmError, match="过期"):
            confirms.consume(pending.scope, pending.code)
        assert not confirms.cancel(pending.scope)


def test_new_issue_replaces_previous_selection_and_code(state, server, choice):
    confirms = state.InstallConfirmStore()
    identity = state.ServerIdentity.from_server(server)
    old = confirms.issue((1, 2, 3), identity, choice)
    new = confirms.issue(old.scope, identity, replace(choice, item_id="other-release"))
    assert old.code != new.code
    with pytest.raises(state.ConfirmError):
        confirms.consume(old.scope, old.code)
    assert confirms.consume(new.scope, new.code).choice.item_id == "other-release"


def test_concurrent_confirmation_consumption_only_succeeds_once(state, server, choice):
    confirms = state.InstallConfirmStore()
    pending = confirms.issue((1, 2, 3), state.ServerIdentity.from_server(server), choice)
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait()
        try:
            confirms.consume(pending.scope, pending.code)
            return True
        except state.ConfirmError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: consume(), range(2))) == [False, True]


def test_begin_stores_only_required_public_metadata(state, store, server, choice):
    store.begin(state.ServerIdentity.from_server(server), choice)
    row = store.get(server.instance_uuid)
    assert row["phase"] == "preparing"
    assert row["pack_name"] == choice.name
    assert row["pack_version"] == choice.version
    assert row["server_created_at"] == server.created_at
    assert row["write_started_at"] == 0
    assert row["baseline_log_stamp"] == ""
    assert row["install_outcome"] == "unknown"
    assert len(row["attempt_id"]) == 32 and int(row["attempt_id"], 16) >= 0
    assert set(row) == {
        "instance_uuid", "card_id", "server_name", "server_created_at", "phase",
        "pack_name", "pack_version", "created_at", "updated_at",
        "write_started_at", "baseline_log_stamp", "install_outcome", "attempt_id",
    }
    content = store.db_path.read_bytes()
    for secret in (server.token, server.account_phone, choice.file_name, choice.search_query):
        assert secret.encode() not in content
    store.ensure_card_available("unrelated-card")
    with pytest.raises(state.MaintenanceError):
        store.ensure_card_available(server.card_id)


@pytest.mark.parametrize("changes", [
    {}, {"instance_uuid": "other-instance"}, {"card_id": "other-card"},
    {"instance_uuid": "ABCD1234", "card_id": "other-card"},
])
def test_duplicate_begin_is_atomic_for_instance_and_card(state, store, server, choice, changes):
    identity = state.ServerIdentity.from_server(server)
    store.begin(identity, choice)
    with pytest.raises(state.MaintenanceError):
        store.begin(replace(identity, **changes), choice)
    assert store.get(identity.instance_uuid)["card_id"] == identity.card_id
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_maintenance").fetchone()[0] == 1


@pytest.mark.parametrize("phase", ["preparing", "submitted", "unknown"])
def test_all_phases_survive_restart_and_never_expire(
    state, store, server, choice, monkeypatch, phase,
):
    store.begin(state.ServerIdentity.from_server(server), choice)
    if phase != "preparing":
        store.mark(server.instance_uuid, phase)
    monkeypatch.setattr(state.time, "time", lambda: 10**12)
    restarted = state.MaintenanceStore(store.db_path)
    restarted.init_db()
    assert restarted.get(server.instance_uuid)["phase"] == phase
    with pytest.raises(state.MaintenanceError):
        restarted.ensure_card_available(server.card_id)


def test_deleted_and_recreated_server_config_does_not_remove_guard(state, store, server, choice):
    identity = state.ServerIdentity.from_server(server)
    store.begin(identity, choice)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("CREATE TABLE servers (name TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO servers VALUES (?)", (server.name,))
        connection.execute("DELETE FROM servers WHERE name = ?", (server.name,))
    with pytest.raises(state.MaintenanceError):
        store.begin(replace(identity, name="replacement", created_at=999), choice)
    with pytest.raises(state.MaintenanceError):
        store.ensure_card_available(server.card_id)


def test_finish_is_explicit_idempotent_and_allows_next_install(state, store, server, choice):
    identity = state.ServerIdentity.from_server(server)
    store.begin(identity, choice)
    store.mark(identity.instance_uuid, "submitted")
    assert not store.finish("unrelated-instance")
    with pytest.raises(state.MaintenanceError):
        store.ensure_card_available(identity.card_id)
    assert store.finish(identity.instance_uuid)
    assert not store.finish(identity.instance_uuid)
    assert store.get(identity.instance_uuid) is None
    store.ensure_card_available(identity.card_id)
    store.begin(identity, choice)
    assert store.get(identity.instance_uuid)["phase"] == "preparing"


def test_concurrent_begin_same_card_only_one_succeeds(state, store, server, choice):
    identity = state.ServerIdentity.from_server(server)
    barrier = threading.Barrier(2)

    def begin(index):
        barrier.wait()
        try:
            state.MaintenanceStore(store.db_path).begin(
                replace(identity, instance_uuid=f"instance-{index}"), choice,
            )
            return True
        except state.MaintenanceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(begin, range(2))) == [False, True]


def test_invalid_phase_and_missing_guard_are_not_silently_accepted(state, store):
    for phase in ("submitted", "unknown", "finished"):
        with pytest.raises(state.MaintenanceError):
            store.mark("missing", phase)


@pytest.mark.parametrize("field", ["card_id", "instance_uuid"])
def test_empty_identity_cannot_create_incomplete_protection(state, store, server, choice, field):
    identity = replace(state.ServerIdentity.from_server(server), **{field: ""})
    with pytest.raises(state.MaintenanceError):
        store.begin(identity, choice)


def test_guard_read_failure_fails_closed(state, tmp_path):
    uninitialized = state.MaintenanceStore(tmp_path / "missing-schema.db")
    with pytest.raises(state.MaintenanceError):
        uninitialized.ensure_card_available("card")
    with pytest.raises(state.MaintenanceError):
        uninitialized.ensure_instance_available("abcd1234")


FULL_ID = "abcd1234-5678-4abc-8def-123456789abc"


@pytest.mark.parametrize("saved,query", [
    (FULL_ID, "abcd1234"), ("abcd1234", FULL_ID),
    (FULL_ID.upper(), "ABCD1234"), ("ABCD1234", FULL_ID.upper()),
    (FULL_ID, FULL_ID.replace("-", "")),
    (FULL_ID.replace("-", ""), FULL_ID),
    (FULL_ID.replace("-", ""), "abcd1234"),
    ("other-instance", "OTHER-INSTANCE"),
])
def test_instance_guard_blocks_valid_aliases(state, store, server, choice, saved, query):
    identity = replace(state.ServerIdentity.from_server(server), instance_uuid=saved)
    store.begin(identity, choice)
    with pytest.raises(state.MaintenanceError):
        store.ensure_instance_available(query)
    assert store.finish(saved)
    store.ensure_instance_available(query)


@pytest.mark.parametrize("query", [
    "abcd", "abcd123", "abcd1234-extra", "abcd1234-0000-4000-8000-000000000000", "",
])
def test_instance_guard_does_not_treat_arbitrary_prefix_as_identity(state, store, server, choice, query):
    store.begin(replace(state.ServerIdentity.from_server(server), instance_uuid=FULL_ID), choice)
    store.ensure_instance_available(query)


def test_non_hex_eight_character_prefix_is_not_a_uuid_alias(state, store, server, choice):
    store.begin(replace(state.ServerIdentity.from_server(server), instance_uuid="zzzz1234-rest"), choice)
    store.ensure_instance_available("zzzz1234")


@pytest.mark.parametrize("saved,new_id", [(FULL_ID, "abcd1234"), ("abcd1234", FULL_ID)])
def test_begin_rejects_different_card_for_same_instance_alias(state, store, server, choice, saved, new_id):
    identity = replace(state.ServerIdentity.from_server(server), instance_uuid=saved)
    store.begin(identity, choice)
    with pytest.raises(state.MaintenanceError):
        store.begin(replace(identity, instance_uuid=new_id, card_id="another-card"), choice)


def test_concurrent_begin_aliases_on_different_cards_are_atomic(state, store, server, choice):
    identity = state.ServerIdentity.from_server(server)
    barrier = threading.Barrier(2)

    def begin(index):
        barrier.wait()
        try:
            state.MaintenanceStore(store.db_path).begin(
                replace(identity, instance_uuid=(FULL_ID, "abcd1234")[index], card_id=f"card-{index}"),
                choice,
            )
            return True
        except state.MaintenanceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(begin, range(2))) == [False, True]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_maintenance").fetchone()[0] == 1


def test_schema_migration_preserves_legacy_unknown_write_boundary(state, tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE modpack_maintenance (
            instance_uuid TEXT PRIMARY KEY COLLATE NOCASE,
            card_id TEXT NOT NULL UNIQUE, server_name TEXT NOT NULL,
            server_created_at INTEGER NOT NULL, phase TEXT NOT NULL,
            pack_name TEXT NOT NULL, pack_version TEXT NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        connection.execute("INSERT INTO modpack_maintenance VALUES (?,?,?,?,?,?,?,?,?)",
                           ("abcd1234", "old-card", "old-server", 1, "unknown", "Pack", "v1", 100, 200))
    migrated = state.MaintenanceStore(path)
    migrated.init_db()
    migrated.init_db()  # Startup migration must be safe to run repeatedly.
    entry = migrated.get("abcd1234")
    assert entry["created_at"] == 100 and entry["updated_at"] == 200
    assert entry["phase"] == "unknown" and entry["install_outcome"] == "unknown"
    assert entry["write_started_at"] is None and entry["baseline_log_stamp"] is None
    assert entry["attempt_id"] == ""
    with pytest.raises(state.MaintenanceError):
        migrated.start_write("abcd1234", "")
    migrated.observe(entry, "completed")
    assert migrated.get("abcd1234")["install_outcome"] == "completed"
    with pytest.raises(state.MaintenanceError):
        migrated.ensure_card_available("old-card")


def test_start_write_is_durable_single_use_without_changing_phase(state, store, server, choice, monkeypatch):
    store.begin(state.ServerIdentity.from_server(server), choice)
    monkeypatch.setattr(state.time, "time", lambda: 1234567890)
    store.start_write(server.instance_uuid.upper(), "public-log-metadata")
    restarted = state.MaintenanceStore(store.db_path)
    restarted.init_db()
    entry = restarted.get(server.instance_uuid)
    assert entry["write_started_at"] == 1234567890
    assert entry["baseline_log_stamp"] == "public-log-metadata"
    assert entry["phase"] == "preparing" and entry["install_outcome"] == "unknown"
    with pytest.raises(state.MaintenanceError):
        restarted.start_write(server.instance_uuid, "replacement-baseline")
    assert restarted.get(server.instance_uuid) == entry


@pytest.mark.parametrize("stamp", [None, True, 1, [], {}, "x" * 2049],
                         ids=["none", "bool", "integer", "list", "dict", "too-long"])
def test_start_write_invalid_baseline_never_marks_request_started(state, store, server, choice, stamp):
    store.begin(state.ServerIdentity.from_server(server), choice)
    with pytest.raises(state.MaintenanceError):
        store.start_write(server.instance_uuid, stamp)
    assert store.get(server.instance_uuid)["write_started_at"] == 0
    assert store.get(server.instance_uuid)["baseline_log_stamp"] == ""


@pytest.mark.parametrize("phase", ["submitted", "unknown"])
def test_start_write_cannot_reenter_a_non_preparing_guard(state, store, server, choice, phase):
    store.begin(state.ServerIdentity.from_server(server), choice)
    store.mark(server.instance_uuid, phase)
    with pytest.raises(state.MaintenanceError):
        store.start_write(server.instance_uuid, "")
    assert store.get(server.instance_uuid)["write_started_at"] == 0


def test_start_write_missing_guard_fails_closed(state, store):
    with pytest.raises(state.MaintenanceError):
        store.start_write("missing", "")


def test_concurrent_start_write_records_exactly_one_baseline(state, store, server, choice):
    store.begin(state.ServerIdentity.from_server(server), choice)
    barrier = threading.Barrier(2)

    def start(index):
        barrier.wait()
        try:
            state.MaintenanceStore(store.db_path).start_write(server.instance_uuid, f"baseline-{index}")
            return index
        except state.MaintenanceError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.get(server.instance_uuid)["baseline_log_stamp"] == f"baseline-{winners[0]}"


@pytest.mark.parametrize("outcome", ["unknown", "installing", "completed", "failed", "not_submitted"])
def test_observation_persists_outcome_but_never_releases_maintenance(state, store, server, choice, outcome):
    store.begin(state.ServerIdentity.from_server(server), choice)
    store.start_write(server.instance_uuid, "baseline")
    store.mark(server.instance_uuid, "unknown")
    original = store.get(server.instance_uuid)
    store.observe(original, outcome)
    restarted = state.MaintenanceStore(store.db_path)
    restarted.init_db()
    assert restarted.get(server.instance_uuid) == {**original, "install_outcome": outcome}
    with pytest.raises(state.MaintenanceError):
        restarted.ensure_card_available(server.card_id)


@pytest.mark.parametrize("outcome", ["success", "submitted", "", None, True, 1])
def test_invalid_observation_outcome_does_not_change_guard(state, store, server, choice, outcome):
    store.begin(state.ServerIdentity.from_server(server), choice)
    entry = store.get(server.instance_uuid)
    with pytest.raises(state.MaintenanceError):
        store.observe(entry, outcome)
    assert store.get(server.instance_uuid) == entry


@pytest.mark.parametrize("field,value", [
    ("created_at", -1), ("write_started_at", -1), ("card_id", "different-card"),
    ("instance_uuid", "another-instance"),
    ("attempt_id", "stale-attempt"),
])
def test_observe_cannot_update_a_different_installation_window(state, store, server, choice, field, value):
    store.begin(state.ServerIdentity.from_server(server), choice)
    store.start_write(server.instance_uuid, "")
    entry = store.get(server.instance_uuid)
    with pytest.raises(state.MaintenanceError):
        store.observe({**entry, field: value}, "completed")
    assert store.get(server.instance_uuid) == entry


def test_late_observation_cannot_resurrect_or_overwrite_new_guard(state, store, server, choice, monkeypatch):
    identity = state.ServerIdentity.from_server(server)
    monkeypatch.setattr(state.time, "time", lambda: 100)
    store.begin(identity, choice)
    store.start_write(server.instance_uuid, "old")
    old = store.get(server.instance_uuid)
    assert store.finish(server.instance_uuid)
    with pytest.raises(state.MaintenanceError):
        store.observe(old, "completed")
    assert store.get(server.instance_uuid) is None
    monkeypatch.setattr(state.time, "time", lambda: 200)
    store.begin(identity, choice)
    store.start_write(server.instance_uuid, "new")
    with pytest.raises(state.MaintenanceError):
        store.observe(old, "completed")
    new = store.get(server.instance_uuid)
    assert new["install_outcome"] == "unknown"
    assert new["write_started_at"] == 200 and new["baseline_log_stamp"] == "new"


def test_install_record_writes_fail_closed_without_schema(state, tmp_path):
    store = state.MaintenanceStore(tmp_path / "missing.db")
    with pytest.raises(state.MaintenanceError):
        store.start_write("abcd1234", "")
    with pytest.raises(state.MaintenanceError):
        store.observe({"instance_uuid": "abcd1234", "created_at": 1, "card_id": "card"}, "unknown")


def test_same_second_recreated_guard_has_distinct_attempt_id(state, store, server, choice, monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 100)
    identity = state.ServerIdentity.from_server(server)
    store.begin(identity, choice)
    store.start_write(server.instance_uuid, "")
    old = store.get(server.instance_uuid)
    store.finish(server.instance_uuid)
    store.begin(identity, choice)
    store.start_write(server.instance_uuid, "")
    new = store.get(server.instance_uuid)
    assert old["created_at"] == new["created_at"] == old["write_started_at"] == new["write_started_at"]
    assert old["attempt_id"] != new["attempt_id"]
    with pytest.raises(state.MaintenanceError):
        store.observe(old, "completed")
    assert store.get(server.instance_uuid) == new


@pytest.mark.parametrize("prior", ["installing", "completed", "failed"])
def test_unknown_observation_keeps_prior_evidence_for_same_attempt(state, store, server, choice, prior):
    store.begin(state.ServerIdentity.from_server(server), choice)
    store.start_write(server.instance_uuid, "")
    entry = store.get(server.instance_uuid)
    store.observe(entry, prior)
    store.observe(store.get(server.instance_uuid), "unknown")
    assert store.get(server.instance_uuid)["install_outcome"] == prior


@pytest.mark.parametrize("reason", ["manual", "completed", "failed", "not_submitted"])
def test_finish_atomically_archives_public_record_and_survives_restart(
    state, store, server, choice, monkeypatch, reason,
):
    store.begin(state.ServerIdentity.from_server(server), choice)
    original = store.get(server.instance_uuid)
    monkeypatch.setattr(state.time, "time", lambda: 2000000000)
    assert store.finish(server.instance_uuid, expected=original, reason=reason)
    assert store.get(server.instance_uuid) is None
    archived = {**original, "released_at": 2000000000, "release_reason": reason}
    assert store.latest(server.instance_uuid) == archived
    restarted = state.MaintenanceStore(store.db_path)
    restarted.init_db()
    assert restarted.latest(server.instance_uuid.upper()) == archived
    restarted.ensure_card_available(server.card_id)
    restarted.ensure_instance_available(server.instance_uuid)
    raw = store.db_path.read_bytes()
    for secret in (server.token, server.account_phone, choice.file_name, choice.search_query):
        assert secret.encode() not in raw


def test_latest_prefers_active_guard_then_most_recent_archive(state, store, server, choice):
    identity = state.ServerIdentity.from_server(server)
    assert store.latest(server.instance_uuid) is None
    store.begin(identity, choice)
    first = store.get(server.instance_uuid)
    store.finish(server.instance_uuid, expected=first, reason="completed")
    store.begin(identity, replace(choice, name="second pack"))
    second = store.get(server.instance_uuid)
    assert store.latest(server.instance_uuid) == second
    assert "released_at" not in second
    store.finish(server.instance_uuid, expected=second, reason="failed")
    latest = store.latest(server.instance_uuid)
    assert latest["attempt_id"] == second["attempt_id"] != first["attempt_id"]
    assert latest["pack_name"] == "second pack" and latest["release_reason"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("attempt_id", "stale-attempt"), ("created_at", -1), ("card_id", "other-card"),
    ("server_created_at", -1), ("write_started_at", -1),
])
def test_finish_expected_snapshot_mismatch_never_deletes_or_archives(
    state, store, server, choice, field, value,
):
    store.begin(state.ServerIdentity.from_server(server), choice)
    original = store.get(server.instance_uuid)
    with pytest.raises(state.MaintenanceError):
        store.finish(server.instance_uuid, expected={**original, field: value}, reason="completed")
    assert store.get(server.instance_uuid) == original
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_history").fetchone()[0] == 0


def test_same_second_stale_finish_cannot_release_new_attempt(state, store, server, choice, monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 100)
    identity = state.ServerIdentity.from_server(server)
    store.begin(identity, choice)
    old = store.get(server.instance_uuid)
    store.finish(server.instance_uuid, expected=old)
    store.begin(identity, choice)
    new = store.get(server.instance_uuid)
    with pytest.raises(state.MaintenanceError):
        store.finish(server.instance_uuid, expected=old, reason="completed")
    assert store.get(server.instance_uuid) == new
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_history").fetchone()[0] == 1


def test_concurrent_finish_expected_snapshot_archives_exactly_once(state, store, server, choice):
    store.begin(state.ServerIdentity.from_server(server), choice)
    entry = store.get(server.instance_uuid)
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        try:
            return state.MaintenanceStore(store.db_path).finish(
                server.instance_uuid, expected=entry, reason="completed",
            )
        except state.MaintenanceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: finish(), range(2))) == [False, True]
    assert store.get(server.instance_uuid) is None
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_history").fetchone()[0] == 1


@pytest.mark.parametrize("failure_stage", ["archive", "delete"])
def test_archive_or_delete_failure_rolls_back_both_operations(state, store, server, choice, failure_stage):
    store.begin(state.ServerIdentity.from_server(server), choice)
    original = store.get(server.instance_uuid)
    table, action = ("modpack_history", "INSERT") if failure_stage == "archive" else ("modpack_maintenance", "DELETE")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            f"CREATE TRIGGER injected_failure BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'private-storage-error'); END"
        )
    with pytest.raises(state.MaintenanceError) as error:
        store.finish(server.instance_uuid, expected=original, reason="completed")
    assert "private-storage-error" not in str(error.value)
    assert store.get(server.instance_uuid) == original
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM modpack_history").fetchone()[0] == 0
    with pytest.raises(state.MaintenanceError):
        store.ensure_card_available(server.card_id)


@pytest.mark.parametrize("query_card,query_instance,blocked", [
    ("test-card", "other-instance", True),
    ("another-card", "abcd1234", True),
    ("another-card", FULL_ID.upper(), True),
    ("another-card", FULL_ID.replace("-", ""), True),
    ("another-card", "abcd1234-extra", False),
    ("another-card", "cafe1234", False),
])
def test_find_blocking_matches_card_or_valid_instance_alias_only_active(
    state, store, server, choice, query_card, query_instance, blocked,
):
    identity = replace(state.ServerIdentity.from_server(server), instance_uuid=FULL_ID)
    store.begin(identity, choice)
    original = store.get(FULL_ID)
    assert store.find_blocking(query_card, query_instance) == (original if blocked else None)
    store.finish(FULL_ID, expected=original, reason="completed")
    assert store.find_blocking(query_card, query_instance) is None


@pytest.mark.parametrize("reason", ["arbitrary-secret", "", None, True, 1])
def test_invalid_archive_reason_never_releases_guard(state, store, server, choice, reason):
    store.begin(state.ServerIdentity.from_server(server), choice)
    original = store.get(server.instance_uuid)
    with pytest.raises(state.MaintenanceError):
        store.finish(server.instance_uuid, expected=original, reason=reason)
    assert store.get(server.instance_uuid) == original


def test_latest_and_find_blocking_storage_errors_fail_closed(state, tmp_path):
    store = state.MaintenanceStore(tmp_path / "missing-history.db")
    with pytest.raises(state.MaintenanceError):
        store.latest("abcd1234")
    with pytest.raises(state.MaintenanceError):
        store.find_blocking("card", "abcd1234")


def test_invalid_history_json_never_looks_like_a_finished_install(state, store):
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("INSERT INTO modpack_history(instance_uuid,record) VALUES (?,?)", ("abcd1234", "not JSON"))
    with pytest.raises(state.MaintenanceError):
        store.latest("abcd1234")
