"""Publishing completed parts must update the viewer before the full run finishes."""

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from docgen.core.export import dataset_card, merge_stats

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parts_runner = load_script("run_in_parts")
uploader = load_script("push_to_hf")


def part_stats(t, split, scans=0):
    return {"format": "parquet", "pages": 1, "documents": {split: 1},
            "scans": {split: scans} if scans else {}, "by_type": {t: 1},
            "by_source": {"northwind": 1}, "by_locale": {"en": 1}, "by_layout": {"classic/plain": 1},
            "by_type_split": {t: {split: 1}}, "by_type_source": {t: {"northwind": 1}},
            "companies": {split: ["example"]}}


class IncrementalCard(unittest.TestCase):
    def test_only_completed_types_and_existing_splits_are_declared(self):
        stats = merge_stats([part_stats("receipt", "train", scans=1), part_stats("payslip", "test")])
        card = dataset_card(stats, repo_id="example/documents")
        configs = {c["config_name"]: c for c in yaml.safe_load(card.split("---")[1])["configs"]}
        self.assertEqual(set(configs), {"all", "receipt", "payslip", "scans"})
        self.assertTrue(configs["all"]["default"])
        self.assertEqual(configs["all"]["data_files"], [
            {"split": "train", "path": ["data/receipt/train-*.parquet"]},
            {"split": "test", "path": ["data/payslip/test-*.parquet"]}])
        self.assertEqual(configs["scans"]["data_files"], [
            {"split": "train", "path": ["scans/receipt/train-*.parquet"]}])
        self.assertNotIn('"invoice", split=', card)

    def test_new_and_older_checkpoints_produce_same_scan_config(self):
        old = part_stats("receipt", "train", scans=1)
        new = copy.deepcopy(old)
        new["by_type_scan_split"] = {"receipt": {"train": 1}}
        self.assertEqual(dataset_card(merge_stats([old])), dataset_card(merge_stats([new])))


class IncrementalPublishing(unittest.TestCase):
    def run_main(self, argv, initial_done, upload):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            with patch.object(parts_runner, "REPO_ROOT", root), \
                    patch.object(parts_runner.rp, "venv_ok", return_value=True), \
                    patch.object(parts_runner.rp, "say"), \
                    patch.object(parts_runner.rp, "LOG_FILE", None), \
                    patch.object(parts_runner, "capacities", return_value={"receipt": 10, "invoice": 20}), \
                    patch.object(parts_runner, "done_types", return_value=initial_done), \
                    patch.object(parts_runner, "do_type", side_effect=upload) as generate, \
                    patch.object(parts_runner, "finish") as publish, \
                    patch("sys.argv", ["run_in_parts.py", *argv]):
                result = parts_runner.main()
        return result, generate, publish

    def test_subset_publishes_while_other_types_are_pending(self):
        done = set()
        result, generate, publish = self.run_main(["--types", "receipt"], done,
                                                  lambda t, n, args: done.add(t))
        self.assertEqual(result, 0)
        generate.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], ["invoice", "receipt"])

    def test_publish_only_refreshes_existing_parts_without_generating(self):
        result, generate, publish = self.run_main(["--publish-only"], {"receipt"}, None)
        self.assertEqual(result, 0)
        generate.assert_not_called()
        publish.assert_called_once()

    def test_trial_never_publishes(self):
        result, generate, publish = self.run_main(["--types", "receipt", "--no-push"], set(), None)
        self.assertEqual(result, 0)
        generate.assert_called_once()
        publish.assert_not_called()


class LegacyDeletion(unittest.TestCase):
    def test_folder_deletes_preserve_shards_and_hub_attributes(self):
        remote = dict.fromkeys([".gitattributes", "pdf/receipt/train/a.pdf", "json/receipt/a.json",
                                "scans/receipt/train/a.jpg", "scans/receipt/train/metadata.jsonl",
                                "scans/receipt/train-00000.parquet", "data/receipt/train-00000.parquet"], 0)
        stale = [p for p in remote if p.startswith(("pdf/", "json/", "scans/receipt/train/"))]
        self.assertEqual(uploader.deletion_paths(remote, stale), ["json/", "pdf/", "scans/receipt/train/"])
        self.assertEqual(uploader.deletion_paths(remote, []), [])
