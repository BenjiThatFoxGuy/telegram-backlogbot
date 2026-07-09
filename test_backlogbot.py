import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import backlogbot


def make_cfg(success_action="delete", legacy_per_target_dedupe=False):
    return backlogbot.BacklogConfig(
        enabled=True,
        backlog_root=Path("/backlog"),
        backlog_roots=[Path("/backlog")],
        archive_root=Path("/backlog_archive"),
        targets_allowlist=[],
        scan_every_seconds=30,
        settle_seconds=30,
        interval_seconds=21600,
        scope="per_target",
        overdue="post_once",
        success_action=success_action,
        allow_unknown_as_document=False,
        skip_quarantine_unmapped_targets=False,
        legacy_per_target_dedupe=legacy_per_target_dedupe,
        immediate_post_on_start=False,
        use_telegram_scheduler=False,
        scheduler_mode="fixed_cadence",
        schedule_ahead_seconds=7 * 24 * 3600,
        min_schedule_delay_seconds=120,
        max_failures=5,
        tz_name="Europe/Warsaw",
        mongo_url="mongodb://localhost:27017",
        backlog_state_db="backlogbot_test",
    )


class SafeDeleteTests(unittest.TestCase):
    def test_deletes_existing_file(self):
        with patch.object(Path, "unlink") as mock_unlink:
            backlogbot.safe_delete(Path("/tmp/does-not-matter.txt"))
            mock_unlink.assert_called_once()

    def test_missing_file_is_noop(self):
        # missing_ok=True path: real Path.unlink(missing_ok=True) on a nonexistent
        # file must not raise.
        backlogbot.safe_delete(Path("/tmp/definitely-does-not-exist-xyz.bin"))

    def test_permission_error_is_caught_and_logged(self):
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            with patch.object(backlogbot.logger, "exception") as mock_log:
                backlogbot.safe_delete(Path("/tmp/locked-file.bin"))
                mock_log.assert_called_once()


class MarkPostSuccessOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_posted_status_persists_even_if_cleanup_raises(self):
        cfg = make_cfg(success_action="delete")
        item = {"_id": "item1", "target_key": "@chan", "rel_path": "chan/file.jpg"}

        call_order = []

        store = AsyncMock()

        async def fake_set_item_status(item_id, status, **fields):
            call_order.append("set_item_status")

        async def fake_update_one(*args, **kwargs):
            call_order.append("targets.update_one")

        store.set_item_status = AsyncMock(side_effect=fake_set_item_status)
        store.targets.update_one = AsyncMock(side_effect=fake_update_one)

        async def fake_handle_success(*args, **kwargs):
            call_order.append("handle_success")
            raise OSError("disk on fire")

        with patch.object(backlogbot, "handle_success", side_effect=fake_handle_success), \
             patch.object(backlogbot, "resolve_media_path", return_value=Path("/backlog/chan/file.jpg")), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(backlogbot.logger, "exception") as mock_log:
            await backlogbot.mark_post_success(cfg, store, item, message_id=42)

        store.set_item_status.assert_called_once()
        args, kwargs = store.set_item_status.call_args
        self.assertEqual(args[1], "posted")

        self.assertIn("set_item_status", call_order)
        self.assertIn("handle_success", call_order)
        self.assertLess(
            call_order.index("set_item_status"),
            call_order.index("handle_success"),
            "DB must be marked posted before local cleanup runs, so a cleanup "
            "failure can never cause the item to be reposted.",
        )
        mock_log.assert_called_once()


class MarkScheduleSuccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_status_persists_before_cleanup_and_marker(self):
        cfg = make_cfg(success_action="archive")
        item = {"_id": "item1", "target_key": "@chan", "rel_path": "chan/file.jpg"}
        scheduled_at = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)

        call_order = []
        store = AsyncMock()

        async def fake_set_item_status(item_id, status, **fields):
            call_order.append("set_item_status")

        async def fake_update_one(*args, **kwargs):
            call_order.append("targets.update_one")

        async def fake_marker(item_id):
            call_order.append("mark_local_success_action_applied")

        async def fake_handle_success(*args, **kwargs):
            call_order.append("handle_success")

        store.set_item_status = AsyncMock(side_effect=fake_set_item_status)
        store.targets.update_one = AsyncMock(side_effect=fake_update_one)
        store.mark_local_success_action_applied = AsyncMock(side_effect=fake_marker)

        with patch.object(backlogbot, "handle_success", side_effect=fake_handle_success), \
             patch.object(backlogbot, "resolve_media_path", return_value=Path("/backlog/chan/file.jpg")), \
             patch.object(Path, "exists", return_value=False):
            await backlogbot.mark_schedule_success(
                cfg,
                store,
                item,
                message_id=99,
                scheduled_at=scheduled_at,
            )

        store.set_item_status.assert_called_once()
        args, kwargs = store.set_item_status.call_args
        self.assertEqual(args[1], "scheduled")
        self.assertEqual(kwargs["scheduled_message_id"], 99)
        self.assertEqual(kwargs["scheduled_at"], scheduled_at)
        self.assertIsNone(kwargs["local_success_action_at"])

        self.assertEqual(
            call_order,
            [
                "set_item_status",
                "targets.update_one",
                "handle_success",
                "mark_local_success_action_applied",
            ],
        )


class CleanupScheduledLocalFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleans_scheduled_items_without_success_marker(self):
        cfg = make_cfg(success_action="archive")
        item = {"_id": "item1", "target_key": "@chan", "rel_path": "chan/file.jpg"}

        class AsyncList:
            def __init__(self, values):
                self.values = list(values)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.values:
                    raise StopAsyncIteration
                return self.values.pop(0)

        class Items:
            def __init__(self, values):
                self.values = values
                self.query = None

            def find(self, query):
                self.query = query
                return AsyncList(self.values)

        class Store:
            def __init__(self):
                self.items = Items([item])
                self.marked = []

            async def mark_local_success_action_applied(self, item_id):
                self.marked.append(item_id)

        store = Store()

        with patch.object(backlogbot, "handle_success", new_callable=AsyncMock) as mock_handle_success, \
             patch.object(backlogbot, "resolve_media_path", return_value=Path("/backlog/chan/file.jpg")), \
             patch.object(Path, "exists", return_value=False):
            await backlogbot.cleanup_scheduled_local_files(cfg, store, target_key="@chan")

        self.assertEqual(store.items.query["status"], "scheduled")
        self.assertEqual(store.items.query["target_key"], "@chan")
        self.assertIn({"local_success_action_at": {"$exists": False}}, store.items.query["$or"])
        mock_handle_success.assert_called_once()
        self.assertEqual(store.marked, ["item1"])


class SendFailureQuarantineTests(unittest.IsolatedAsyncioTestCase):
    def test_photo_save_file_invalid_is_quarantinable(self):
        err = (
            "PhotoSaveFileInvalid: Telegram says: [400 PHOTO_SAVE_FILE_INVALID] "
            "(caused by \"messages.SendMedia\")"
        )

        self.assertEqual(
            backlogbot.quarantine_reason_for_send_error(err),
            "telegram_photo_save_file_invalid",
        )

    def test_peer_id_invalid_is_not_media_quarantine(self):
        err = "PeerIdInvalid: Telegram says: [400 PEER_ID_INVALID]"

        self.assertIsNone(backlogbot.quarantine_reason_for_send_error(err))

    async def test_handle_send_failure_quarantines_file_and_sidecar(self):
        class Store:
            def __init__(self):
                self.status_updates = []

            async def set_item_status(self, *args, **kwargs):
                self.status_updates.append((args, kwargs))

            async def bump_failure(self, *args, **kwargs):
                raise AssertionError("permanent media errors should not be retried")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backlog"
            archive = Path(tmp) / "archive"
            target_dir = root / "@chan"
            target_dir.mkdir(parents=True)
            media = target_dir / "bad.png"
            sidecar = target_dir / "bad.png.caption.txt"
            media.write_bytes(b"bad image")
            sidecar.write_text("caption", encoding="utf-8")

            cfg = replace(make_cfg(), backlog_root=root, backlog_roots=[root], archive_root=archive)
            item = {
                "_id": "item1",
                "target_key": "@chan",
                "rel_path": "@chan/bad.png",
            }
            store = Store()

            with patch.object(backlogbot.logger, "warning"):
                quarantined = await backlogbot.handle_send_failure(
                    cfg,
                    store,
                    item,
                    error="PhotoSaveFileInvalid: Telegram says: [400 PHOTO_SAVE_FILE_INVALID]",
                    context="test",
                )

            quarantine_dir = archive / "_quarantine" / "telegram_photo_save_file_invalid" / "@chan"
            self.assertTrue(quarantined)
            self.assertFalse(media.exists())
            self.assertFalse(sidecar.exists())
            self.assertTrue((quarantine_dir / "bad.png").exists())
            self.assertTrue((quarantine_dir / "bad.png.caption.txt").exists())

            args, kwargs = store.status_updates[0]
            self.assertEqual(args, ("item1", "quarantined"))
            self.assertEqual(kwargs["quarantine_reason"], "telegram_photo_save_file_invalid")
            self.assertIsNone(kwargs["scheduled_message_id"])
            self.assertIsNone(kwargs["scheduled_at"])


class SchedulerReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_pyrofork_scheduled_messages_by_tracked_ids(self):
        cfg = make_cfg()
        item = {
            "_id": "item1",
            "target_key": "-1004448064069",
            "rel_path": "chan/file.jpg",
            "status": "scheduled",
            "scheduled_message_id": 99,
            "scheduled_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        }

        class Message:
            id = 99

        class AsyncList:
            def __init__(self, values):
                self.values = list(values)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.values:
                    raise StopAsyncIteration
                return self.values.pop(0)

        class Items:
            def __init__(self):
                self.queries = []

            def find(self, query):
                self.queries.append(query)
                return AsyncList([item])

        class Store:
            def __init__(self):
                self.items = Items()
                self.status_updates = []

            async def set_item_status(self, *args, **kwargs):
                self.status_updates.append((args, kwargs))

        class App:
            def __init__(self):
                self.calls = []

            async def get_scheduled_messages(self, chat_id, message_ids):
                self.calls.append((chat_id, list(message_ids)))
                return [Message()]

        store = Store()
        app = App()

        await backlogbot.scheduler_reconcile(cfg, app, store, target_key="-1004448064069")

        self.assertEqual(app.calls, [(-1004448064069, [99])])
        self.assertEqual(store.status_updates, [])
        self.assertEqual(len(store.items.queries), 2)

    async def test_fetches_scheduled_messages_with_cached_peer_id_for_bare_numeric_target(self):
        cfg = make_cfg()
        item = {
            "_id": "item1",
            "target_key": "4448064069",
            "rel_path": "chan/file.jpg",
            "status": "scheduled",
            "scheduled_message_id": 99,
            "scheduled_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        }

        class Message:
            id = 99

        class AsyncList:
            def __init__(self, values):
                self.values = list(values)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.values:
                    raise StopAsyncIteration
                return self.values.pop(0)

        class Items:
            def find(self, query):
                return AsyncList([item])

        class Store:
            def __init__(self):
                self.items = Items()
                self.status_updates = []

            async def get_or_create_target(self, target_key):
                return {"_id": target_key, "peer_id": -1004448064069}

            async def set_item_status(self, *args, **kwargs):
                self.status_updates.append((args, kwargs))

        class App:
            def __init__(self):
                self.calls = []

            async def get_scheduled_messages(self, chat_id, message_ids):
                self.calls.append((chat_id, list(message_ids)))
                return [Message()]

        store = Store()
        app = App()

        await backlogbot.scheduler_reconcile(cfg, app, store, target_key="4448064069")

        self.assertEqual(app.calls, [(-1004448064069, [99])])
        self.assertEqual(store.status_updates, [])

    async def test_reconcile_supports_older_one_argument_scheduled_getter(self):
        cfg = make_cfg()
        item = {
            "_id": "item1",
            "target_key": "@chan",
            "rel_path": "chan/file.jpg",
            "status": "scheduled",
            "scheduled_message_id": 99,
            "scheduled_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        }

        class Message:
            id = 99

        class AsyncList:
            def __init__(self, values):
                self.values = list(values)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.values:
                    raise StopAsyncIteration
                return self.values.pop(0)

        class Items:
            def find(self, query):
                return AsyncList([item])

        class Store:
            def __init__(self):
                self.items = Items()
                self.status_updates = []

            async def set_item_status(self, *args, **kwargs):
                self.status_updates.append((args, kwargs))

        class App:
            def __init__(self):
                self.calls = []

            async def get_scheduled_messages(self, chat_id):
                self.calls.append(chat_id)
                return [Message()]

        store = Store()
        app = App()

        await backlogbot.scheduler_reconcile(cfg, app, store, target_key="@chan")

        self.assertEqual(app.calls, ["@chan"])
        self.assertEqual(store.status_updates, [])


class ConfigTests(unittest.TestCase):
    def test_legacy_per_target_dedupe_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = backlogbot.load_config()

        self.assertFalse(cfg.legacy_per_target_dedupe)

    def test_legacy_per_target_dedupe_can_be_enabled(self):
        with patch.dict(os.environ, {"BACKLOG_LEGACY_PER_TARGET_DEDUPE": "true"}, clear=True):
            cfg = backlogbot.load_config()

        self.assertTrue(cfg.legacy_per_target_dedupe)


class FindExistingContentItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_dedupe_omits_target_filter_and_returns_posted_first(self):
        class Items:
            def __init__(self):
                self.queries = []

            async def find_one(self, query, projection=None):
                self.queries.append((query, projection))
                if query.get("status") == "posted":
                    return {"target_key": "@other", "rel_path": "other/file.jpg", "status": "posted"}
                return None

        store = object.__new__(backlogbot.BacklogStore)
        store.items = Items()

        existing = await store.find_existing_content_item(target_key=None, sha256="abc123")

        self.assertEqual(existing["target_key"], "@other")
        self.assertEqual(store.items.queries[0][0], {"sha256": "abc123", "status": "posted"})
        self.assertNotIn("target_key", store.items.queries[0][0])
        self.assertEqual(store.items.queries[0][1]["target_key"], 1)

    async def test_legacy_dedupe_filters_by_target_key(self):
        class Items:
            def __init__(self):
                self.queries = []

            async def find_one(self, query, projection=None):
                self.queries.append((query, projection))
                return None

        store = object.__new__(backlogbot.BacklogStore)
        store.items = Items()

        existing = await store.find_existing_content_item(target_key="@chan", sha256="abc123")

        self.assertIsNone(existing)
        self.assertEqual(store.items.queries[0][0]["target_key"], "@chan")
        self.assertEqual(store.items.queries[0][0]["status"], "posted")
        self.assertEqual(store.items.queries[1][0]["target_key"], "@chan")
        self.assertEqual(store.items.queries[1][0]["status"], {"$in": ["pending", "scheduled"]})


class ScanBacklogDedupeScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_uses_global_dedupe_by_default(self):
        store = await self._scan_with_legacy_setting(False)

        self.assertEqual(store.dedupe_target_keys, [None])

    async def test_scan_uses_target_dedupe_in_legacy_mode(self):
        store = await self._scan_with_legacy_setting(True)

        self.assertEqual(store.dedupe_target_keys, ["@chan"])

    async def _scan_with_legacy_setting(self, legacy_per_target_dedupe):
        class Store:
            def __init__(self):
                self.dedupe_target_keys = []

            async def get_or_create_target(self, target_key):
                return {"_id": target_key}

            async def find_existing_content_item(self, *, target_key, sha256):
                self.dedupe_target_keys.append(target_key)
                return None

            async def upsert_item_discovered(self, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_dir = root / "@chan"
            target_dir.mkdir()
            (target_dir / "file.jpg").write_bytes(b"same bytes")

            cfg = replace(
                make_cfg(legacy_per_target_dedupe=legacy_per_target_dedupe),
                backlog_root=root,
                backlog_roots=[root],
                targets_allowlist=["@chan"],
                settle_seconds=0,
            )
            store = Store()

            await backlogbot.scan_backlog(cfg, store, app=None)

        return store


class LeftoverPostedDuplicateHelperTests(unittest.TestCase):
    def test_same_rel_path_is_leftover_not_duplicate(self):
        existing = {"target_key": "@chan", "status": "posted", "rel_path": "chan/file.jpg"}
        self.assertTrue(backlogbot.is_leftover_of_posted_item(existing, "@chan", "chan/file.jpg"))

    def test_different_rel_path_is_genuine_duplicate(self):
        existing = {"target_key": "@chan", "status": "posted", "rel_path": "chan/original.jpg"}
        self.assertFalse(backlogbot.is_leftover_of_posted_item(existing, "@chan", "chan/copy.jpg"))

    def test_different_target_is_genuine_duplicate(self):
        existing = {"target_key": "@other", "status": "posted", "rel_path": "chan/file.jpg"}
        self.assertFalse(backlogbot.is_leftover_of_posted_item(existing, "@chan", "chan/file.jpg"))


if __name__ == "__main__":
    unittest.main()
