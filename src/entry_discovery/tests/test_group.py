import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prepare_group import make_worklist, save_worklist
from discovery import load_ids_file, ensure_checkpoint_identity


class TestGroup(unittest.TestCase):
    def test_combine_overlap_and_provenance(self):
        cfg = {'members': [
            {'member': 'a', 'ranges': [{'start': '2609.12834', 'end': '2609.12836'}]},
            {'member': 'b', 'ranges': [{'start': '2609.12836', 'end': '2609.12838'}]},
            {'member': 'c', 'ranges': [{'start': '2610.00001', 'end': '2610.00002'}]},
            {'member': 'd', 'ranges': [{'start': '2610.00002', 'end': '2610.00002'}]},
        ]}
        r = make_worklist(cfg)
        self.assertEqual(len(r['ids']), 7)
        self.assertEqual(r['owners']['2609.12836'], ['a', 'b'])
        self.assertEqual(r['owners']['2610.00002'], ['c', 'd'])
        self.assertEqual(r['stats']['shared_ids'], 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            save_worklist(r, out)
            self.assertEqual(load_ids_file(out / 'group_ids.txt'), r['ids'])
            self.assertTrue((out / 'id_ownership.csv').exists())

    def test_requires_four_configured_members(self):
        cfg = json.loads((Path(__file__).resolve().parents[1] / 'team_ranges.json').read_text())
        with self.assertRaisesRegex(ValueError, 'missing assigned range'):
            make_worklist(cfg)
        self.assertEqual(len(make_worklist(cfg, allow_incomplete=True)['ids']), 5000)

    def test_manifest_blocks_wrong_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ensure_checkpoint_identity(out, ['2609.12834', '2609.12835'], 20)
            ensure_checkpoint_identity(out, ['2609.12834', '2609.12835'], 20)
            with self.assertRaises(ValueError):
                ensure_checkpoint_identity(out, ['2609.12834'], 20)
            with self.assertRaises(ValueError):
                ensure_checkpoint_identity(out, ['2609.12834', '2609.12835'], 10)

    def test_reject_duplicate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'ids.txt'
            p.write_text('2609.12834\n2609.12834\n')
            with self.assertRaisesRegex(ValueError, 'duplicates'):
                load_ids_file(p)


if __name__ == '__main__':
    unittest.main()
