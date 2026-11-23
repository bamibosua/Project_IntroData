"""Regression tests for metadata boundaries and escaped control tokens."""
import unittest

from .cleanup_leaves import math_token, transform


def outline(text):
    return {
        'publication_key': 'paper', 'version': 1, 'status': 'ok',
        'root_node_id': 'root',
        'nodes': [{'node_id': 'root', 'kind': 'document', 'parent_id': None, 'order': 0}],
        'body_segments': [{'parent_id': 'root', 'span': [0, len(text)]}],
    }


def leaves(text, kind='sentence'):
    result = transform(text, outline(text))
    return [n['content'] for n in result['nodes'] if n['kind'] == kind]


class CleanupRegressionTests(unittest.TestCase):
    def test_duplicate_spans_and_invalid_root_rejected(self):
        text = 'First. Second.'
        data = outline(text)
        data['body_segments'] *= 2
        with self.assertRaises(ValueError):
            transform(text, data)
        data = outline(text)
        data['root_node_id'] = 'missing'
        with self.assertRaises(ValueError):
            transform(text, data)

    def test_graphics_preserve_following_text_and_avoid_double_count(self):
        text = r'\includegraphics{a} {Following text.}'
        self.assertEqual(leaves(text), ['Following text.'])
        self.assertEqual(len(leaves(text, 'figure')), 1)
        text = r'\begin{wrapfigure}{r}{.5\textwidth}\includegraphics{a}\end{wrapfigure}'
        self.assertEqual(len(leaves(text, 'figure')), 1)
        self.assertEqual(leaves(text), [])
        text = r'Use \texttt{\includegraphics{example}} in code.'
        self.assertEqual(leaves(text, 'figure'), [])
        self.assertEqual(len(leaves(text)), 1)

    def test_description_term_reaches_sentence(self):
        text = r'\begin{description}\item[Primary key] Identifies rows.\end{description}'
        self.assertEqual(leaves(text), ['Primary key: Identifies rows.'])

    def test_reference_heading_variants(self):
        for title in ['References:', 'Works Cited']:
            with self.subTest(title=title):
                text = 'Reference entry.'
                data = outline(text)
                data['nodes'].append({'node_id': 'refs', 'kind': 'heading',
                                      'parent_id': 'root', 'title_raw': title})
                data['body_segments'][0]['parent_id'] = 'refs'
                self.assertEqual(transform(text, data)['total_leaves'], 0)

    def test_citation_boundary_and_literal_document_opener(self):
        self.assertEqual(leaves(r'First.\cite{a} \cite{b} Second.'),
                         [r'First.\cite{a} \cite{b}', 'Second.'])
        text = r'Before \verb|\begin{document}| after.'
        self.assertEqual(leaves(text), [text])

    def test_metadata_preserves_following_prose_group(self):
        for command in [r'\date{}', r'\author{A}', r'\title[Short]{Long}',
                        r'\author[1]{A {Nested} Name}', r'\thanks{Support}',
                        r'\usepackage[option]{package}']:
            with self.subTest(command=command):
                self.assertEqual(leaves(command + ' {Important opening.} End.'),
                                 ['Important opening.', 'End.'])

    def test_metadata_inside_document_keeps_opening(self):
        text = r'\begin{document}\author{A} {Opening text.} Next.\end{document}'
        self.assertEqual(leaves(text), ['Opening text.', 'Next.'])

    def test_escaped_environment_is_prose(self):
        for env in ['equation', 'figure', 'thebibliography', 'abstract']:
            with self.subTest(env=env):
                text = 'Before ' + r'\\begin{' + env + '} x=1 After.'
                result = transform(text, outline(text))
                self.assertEqual(result['cleanup_status'], 'completed')
                self.assertEqual(result['leaf_counts']['block_formula'], 0)
                self.assertEqual(result['leaf_counts']['figure'], 0)
                self.assertTrue(any('x=1 After.' in n.get('content', '')
                                    for n in result['nodes'] if n['kind'] == 'sentence'))

    def test_math_token_ignores_escaped_opener(self):
        for text in [r'\\begin{equation} x=1', r'\$5', r'\\[2pt]']:
            with self.subTest(text=text):
                self.assertIsNone(math_token(text, 1))

    def test_genuine_math_after_linebreak_is_still_extracted(self):
        text = r'Before. \\\begin{equation}x=1\end{equation} After.'
        self.assertEqual(len(leaves(text, 'block_formula')), 1)
        self.assertEqual(leaves(text), ['Before.', 'After.'])

    def test_genuine_unclosed_math_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'Unclosed math environment'):
            leaves(r'Before \begin{equation}x=1')

    def test_linebreak_spacing_and_escaped_currency(self):
        self.assertEqual(leaves(r'First\\[2pt] next. Price \$5.'),
                         ['First [2pt] next.', r'Price \$5.'])


if __name__ == '__main__':
    unittest.main()
