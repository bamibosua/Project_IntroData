import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from discovery import generate_ids, parse_atom

XML = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><id>http://arxiv.org/abs/2609.12834v3</id><title>  A\n Study  </title><published>2026-09-10T00:00:00Z</published><updated>2026-10-02T00:00:00Z</updated><author><name>Alice</name></author><arxiv:journal_ref>Journal X</arxiv:journal_ref></entry></feed>'''

class TestDiscovery(unittest.TestCase):
    def test_range(self):
        ids = generate_ids('2609.12834', '2609.17833')
        self.assertEqual((len(ids), ids[0], ids[-1]), (5000, '2609.12834', '2609.17833'))
    def test_bad_range(self):
        with self.assertRaises(ValueError):
            generate_ids('2609.17833','2609.12834')
    def test_cross_month_rejected(self):
        with self.assertRaises(ValueError):
            generate_ids('2609.12834','2610.00001')
    def test_parse(self):
        paper = parse_atom(XML)['2609.12834']
        self.assertEqual(paper['versions'], ['2609.12834v1','2609.12834v2','2609.12834v3'])
        self.assertEqual(paper['title'], 'A Study')
        self.assertEqual(paper['venue'], 'Journal X')
        self.assertIsNone(paper['revised_dates'])

if __name__ == '__main__': unittest.main()
