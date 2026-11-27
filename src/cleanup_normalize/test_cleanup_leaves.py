"""Regression tests for interval brackets swallowed by the cleanup scanner."""
import json
from pathlib import Path
import unittest

import cleanup_leaves as cleanup


class MathIntervalTests(unittest.TestCase):
    def test_relation_does_not_consume_half_open_interval(self):
        text = r'\in[-2L,0),\end{equation} later [0,1]'
        self.assertEqual(cleanup.macro_end(text, 0), len(r'\in'))

    def test_unknown_command_does_not_guess_optional_arguments(self):
        text = r'\customrelation[-2L,0)\end{equation} later ]'
        self.assertEqual(cleanup.macro_end(text, 0), len(r'\customrelation'))

    def test_environment_ends_before_later_brackets(self):
        text = (r'\begin{equation}s(\theta)=\begin{cases}'
                r'\theta+2L,&\theta\in[-2L,0),\\[2pt]'
                r'\theta-2L,&\theta\in[0,2L).\end{cases}\end{equation}'
                r' Follow-up text $\varepsilon\in(0,1]$.')
        end = cleanup.find_env_end(text, 0, 'equation')
        self.assertIsNotNone(end)
        self.assertEqual(text[end[0]:end[1]], r'\end{equation}')
        self.assertTrue(text[end[1]:].startswith(' Follow-up'))
        normalized = cleanup.normalize_text(text)
        self.assertIn(r'\in[-2L,0)', normalized)
        self.assertIn('Follow-up text', normalized)

    def test_interval_after_required_argument_is_not_an_option(self):
        for command in (r'\mathbb{R}', r'\sqrt{2}', r'\cite{key}'):
            text = command + r'[0,1)\end{equation} later ]'
            self.assertEqual(cleanup.macro_end(text, 0), len(command))

    def test_known_optional_arguments_are_still_opaque(self):
        for command in (r'\sqrt[3]{x}', r'\citep[see][p. 2]{key}',
                        r'\includegraphics[width=2cm]{figure}', r'\\[2pt]'):
            self.assertEqual(cleanup.macro_end(command + ' next', 0), len(command))

    def test_macro_definition_and_verbatim_do_not_close_environment(self):
        text = (r'\begin{equation}\def\example{\end{equation}}'
                r'\verb|\end{equation}| x=1\end{equation} tail')
        end = cleanup.find_env_end(text, 0, 'equation')
        self.assertIsNotNone(end)
        self.assertEqual(text[end[1]:], ' tail')

    def test_real_unclosed_math_still_fails(self):
        for text in (r'\begin{equation}x=1', r'$x=1'):
            with self.assertRaises(ValueError):
                cleanup.normalize_text(text)

    def test_list_with_interval_preserves_formula_and_following_item(self):
        merged = (r'\begin{document}\begin{enumerate}'
                  r'\item First \[x\in[-2L,0)\]'
                  r'\item Second $y\in(0,1]$.\end{enumerate}\end{document}')
        root = 'test@v1#document'
        outline = {'publication_key': 'test', 'version': 1, 'status': 'ok',
                   'root_node_id': root,
                   'nodes': [{'node_id': root, 'kind': 'document', 'parent_id': None}],
                   'body_segments': [{'parent_id': root, 'span': [0, len(merged)]}]}
        result = cleanup.transform(merged, outline)
        self.assertEqual(result['cleanup_status'], 'completed')
        self.assertEqual(result['leaf_counts']['block_formula'], 1)
        self.assertEqual(sum(n['kind'] == 'item' for n in result['nodes']), 2)
        self.assertTrue(any('Second' in n.get('content', '') for n in result['nodes']))


class LatexSyntaxTests(unittest.TestCase):
    def test_spaced_environment_end_preserves_following_prose(self):
        text = r'\begin {eqnarray}x&=&1\end {eqnarray} After.'
        end, form, body = cleanup.math_token(text, 0)
        self.assertEqual(text[end:], ' After.')
        self.assertEqual(form, 'eqnarray')
        self.assertEqual(body, 'x&=&1')
        self.assertIn('After.', cleanup.normalize_text(text))

    def test_alignat_unbraced_argument_is_exactly_one_token(self):
        for body, expected in [('1x&=y', '{1}\nx&=y'),
                               ('12x&=y', '{1}\n2x&=y'),
                               (r'\countpairs x&=y', '{\\countpairs}\nx&=y'),
                               ('{12}x&=y', '{12}\nx&=y')]:
            with self.subTest(body=body):
                self.assertIn(expected, cleanup.canonical_math('alignat', body)[1])

    def test_mixed_inline_delimiters_close_at_the_first_valid_token(self):
        for text in (r'\(x$ after $y$', r'$x\) after \(y\)'):
            end, form, body = cleanup.math_token(text, 0)
            self.assertEqual((form, body), ('inline', 'x'))
            self.assertTrue(text[end:].startswith(' after '))
            self.assertEqual(cleanup.normalize_text(text), '$x$ after $y$')

    def test_escaped_and_grouped_dollars_do_not_end_outer_formula(self):
        for text in (r'\(x+\text{cost \$5}$ tail',
                     r'$x+\text{inner $y$}$ tail'):
            end, _, body = cleanup.math_token(text, 0)
            self.assertEqual(text[end:], ' tail')
            self.assertIn('text{', body)

    def test_mixed_delimiters_do_not_hide_unclosed_or_nested_math(self):
        for text in (r'\(x', r'$x', r'\(x \(y\)', r'$x$$'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                cleanup.normalize_text(text)

    def test_declaration_alias_is_proven_and_body_stays_opaque(self):
        text = (r'\newcommand{\nc}{\newcommand}'
                r'\nc\beq{\begin{equation}}\nc\eeq{\end{equation}}')
        aliases = cleanup.declaration_aliases(text)
        self.assertEqual(aliases, {r'\nc': r'\newcommand'})
        expanded = cleanup.expand_declaration_aliases(text, aliases)
        self.assertIn(r'\newcommand\beq{\begin{equation}}', expanded)
        self.assertEqual(cleanup.normalize_text(expanded), '')
        self.assertEqual(cleanup.math_aliases(expanded, len(expanded))[r'\beq'],
                         r'\begin{equation}')

    def test_unknown_or_conflicting_declaration_alias_is_not_guessed(self):
        for text in (r'\nc\beq{\begin{equation}}',
                     r'\newcommand{\nc}{\newcommand}\renewcommand{\nc}[1]{#1}',
                     r'\verb|\newcommand{\nc}{\newcommand}|'):
            self.assertEqual(cleanup.declaration_aliases(text), {})

    def test_body_declaration_alias_transform_preserves_source_spans(self):
        text = (r'\begin{document}\newcommand{\nc}{\newcommand}'
                r'\nc\beq{\begin{equation}}\nc\eeq{\end{equation}}'
                r'Before. \beq x=1\eeq After.\end{document}')
        root = 'test@v1#document'
        outline = {'publication_key': 'test', 'version': 1, 'status': 'ok',
                   'root_node_id': root,
                   'nodes': [{'node_id': root, 'kind': 'document', 'parent_id': None}],
                   'body_segments': [{'parent_id': root, 'span': [0, len(text)]}]}
        result = cleanup.transform(text, outline)
        self.assertEqual(result['leaf_counts']['block_formula'], 1)
        formulas = [n for n in result['nodes'] if n['kind'] == 'block_formula']
        self.assertIn('x=1', formulas[0]['content'])
        self.assertEqual(formulas[0]['source_segment_span'], [0, len(text)])
        self.assertFalse(any('newcommand' in n.get('content', '') for n in result['nodes']))

    def test_four_reported_publications(self):
        root = Path(__file__).resolve().parents[1] / 'parsed_structure/parsed_structure'
        for aid in ('2609-16649', '2608-11904', '2511-15876', '2511-13671'):
            folder = root / aid / 'v1'
            if not folder.exists():
                self.skipTest('Local parsed dataset not available')
            with self.subTest(arxiv_id=aid):
                outline = json.loads((folder / 'outline.json').read_text(encoding='utf-8'))
                result = cleanup.transform((folder / 'merged.tex').read_text(encoding='utf-8'), outline)
                self.assertEqual(result['cleanup_status'], 'completed')
                self.assertGreater(result['total_leaves'], 0)


if __name__ == '__main__':
    unittest.main()
