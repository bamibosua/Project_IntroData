import importlib.util
from pathlib import Path
import tempfile
import unittest
import json
from collections import Counter

spec = importlib.util.spec_from_file_location('cleaning', Path(__file__).with_name('cleaning.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CleaningTests(unittest.TestCase):
    def test_identifiers(self):
        self.assertEqual(c.arxiv_id('https://arxiv.org/abs/2608.9007v2'), '2608.09007')
        self.assertEqual(c.arxiv_id('2609-12844'), '2609.12844')
        self.assertEqual(c.arxiv_id('hep-th/9901001v3'), 'hep-th/9901001')
        self.assertIsNone(c.arxiv_id('2613.12345'))
        self.assertIsNone(c.arxiv_id(None))

    def test_bibtex_nested_fields_and_duplicates_retained(self):
        source = '''@article{key, title={A {Nested} title, with comma},
                    author={Doe, Jane and {Research and Development}}, year=2024}
                    @misc{key, title="Different title"}'''
        entries = list(c.parse_bib(source))
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(error is None for _, error in entries))
        self.assertEqual(entries[0][0]['title'], 'A {Nested} title, with comma')
        self.assertEqual(c.authors(entries[0][0]['author'], bib=True),
                         ['Doe, Jane', 'Research and Development'])

    def test_unsupported_macro_is_explicit_failure(self):
        entries = list(c.parse_bib('@misc{k,title=unknownmacro}'))
        self.assertIn('unresolved', entries[0][1])
        self.assertIn('unresolved', list(c.parse_bib('@misc{k,title={one} # {two}}'))[0][1])
        self.assertIsNotNone(list(c.parse_bib('@misc{k,title={unterminated}'))[0][1])
        recovered = list(c.parse_bib('@misc{bad,title={unterminated}\n@misc{good,title={Valid}}'))
        self.assertIsNotNone(recovered[0][1])
        self.assertEqual(recovered[1][0]['ID'], 'good')
        self.assertIsNone(recovered[1][1])

    def test_conflicting_duplicate_never_arbitrarily_selected(self):
        p = c.Pipeline.__new__(c.Pipeline)
        issues = []
        p.issue = lambda *args: issues.append(args)
        self.assertIsNone(p.merge([{'title': 'A'}, {'title': 'B'}], 'k', '2609.12844', 'conflict'))
        self.assertEqual(len(issues), 1)
        self.assertEqual(p.merge([{'title': None}, {'title': 'A'}], 'k', '2609.12844', 'conflict'),
                         {'title': 'A'})

    def test_parquet_null_contract_and_empty_output(self):
        with tempfile.TemporaryDirectory() as folder:
            options = {'batch_rows': 2, 'shard_rows': 2, 'compression': 'zstd'}
            sink = c.Sink(Path(folder), 'references', options)
            sink.close()
            table = c.pq.read_table(Path(folder) / 'references_clean.parquet')
            self.assertEqual(table.num_rows, 0)
            self.assertFalse(table.schema.field('ref_arxiv_id').nullable)

    def test_fulltext_foreign_keys_are_scoped_to_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            elements = [
                {'arxiv_id': '2609.12844', 'element_id': 'root', 'kind': 'document', 'content': 'root'},
                {'arxiv_id': '2609.12845', 'element_id': 'foreign', 'kind': 'sentence', 'content': 'text'}]
            edges = [{'arxiv_id': '2609.12844', 'version': 1, 'element_id': 'foreign', 'parent_id': 'root'}]
            for name, rows in [('elements', elements), ('hierarchy', edges)]:
                (source / (name + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in rows))
            p = c.Pipeline.__new__(c.Pipeline)
            p.paths, p.out = {'fulltext_source': source}, root / 'output'
            p.out.mkdir()
            p.opts = {'export_fulltext': True, 'batch_rows': 2, 'shard_rows': 2, 'compression': 'zstd'}
            p.owners = dict.fromkeys(['2609.12844', '2609.12845'])
            p.sinks, p.counts, p.coverage = {}, Counter(), {}
            try:
                with self.assertRaisesRegex(ValueError, 'orphan'):
                    p.fulltext()
            finally:
                for sink in p.sinks.values(): sink.close()
            self.assertFalse((p.out / '_validation.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
