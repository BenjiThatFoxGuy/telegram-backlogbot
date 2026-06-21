import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import backlogbot


def make_cfg(success_action="delete"):
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


class LeftoverPostedDuplicateHelperTests(unittest.TestCase):
    def test_same_rel_path_is_leftover_not_duplicate(self):
        existing = {"status": "posted", "rel_path": "chan/file.jpg"}
        self.assertTrue(backlogbot.is_leftover_of_posted_item(existing, "chan/file.jpg"))

    def test_different_rel_path_is_genuine_duplicate(self):
        existing = {"status": "posted", "rel_path": "chan/original.jpg"}
        self.assertFalse(backlogbot.is_leftover_of_posted_item(existing, "chan/copy.jpg"))


if __name__ == "__main__":
    unittest.main()
