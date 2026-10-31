"""Keep typed parameter braces from masquerading as function bodies."""
import unittest

from lambdas.LlamaPReviewPipeline.context_engine.code_extractor import CodeContextExtractor


class TypedFunctionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.extractor = CodeContextExtractor()

    def extract(self, text, symbol, **kwargs):
        lines = text.splitlines()
        selected = self.extractor.pick_representative_line(
            [i for i, line in enumerate(lines) if symbol in line], lines, symbol
        )
        return self.extractor.extract_enclosing_block(text, selected, symbol, **kwargs)

    def test_inline_parameter_type_does_not_end_at_signature(self):
        text = '''export function buildState(
  source: { id: number; body: string } | null,
  existing: readonly string[],
): State {
  if (!source) return emptyState();
  return { id: source.id, complete: existing.length > 0 };
}
export function unrelated() { return false; }
'''
        block, start, end = self.extract(text, 'buildState')
        self.assertEqual((start, end), (1, 7))
        self.assertIn('if (!source)', block)
        self.assertIn('complete: existing.length', block)
        self.assertNotIn('unrelated', block)

    def test_requested_body_anchor_is_not_outside_returned_block(self):
        text = '''export function complete(
  files: ReadonlyArray<{
    name: string;
    patch?: string;
  }>,
  expected: number,
): boolean {
  if (files.length >= FILE_CAP) return false;
  return files.every((f) => Boolean(f.patch));
}
'''
        block, start, end = self.extract(text, 'FILE_CAP')
        self.assertEqual((start, end), (1, 10))
        self.assertIn('FILE_CAP', block)
        self.assertIn('files.every', block)

    def test_nested_callback_defaults_literals_and_comments(self):
        text = '''export async function load(
  options: { get: (x: { id: number }) => Promise<string> },
  label = ") }", // a closing delimiter in a literal is not structural
  defaults = { text: `value ) }` },
): Promise<string> {
  /* ordinary body behavior stays unchanged */
  const value = await options.get({ id: 1 });
  return value + label + defaults.text;
}
const next = 1;
'''
        block, start, end = self.extract(text, 'load')
        self.assertEqual((start, end), (1, 9))
        self.assertIn('return value', block)
        self.assertNotIn('const next', block)

    def test_regex_quote_in_body_does_not_absorb_a_later_function(self):
        text = """function quotedRegex() {
  const quote = /'/;
  return true;
}
const unrelated = 'outside';
function later() {
  return false;
}"""
        block, start, end = self.extract(text, 'quotedRegex')
        self.assertEqual((start, end), (1, 4))
        self.assertNotIn('unrelated', block)
        self.assertNotIn('later', block)

    def test_existing_full_line_comment_skip_is_preserved(self):
        text = 'function ordinary() {\n  // } is not a boundary\n  return true;\n}'
        self.assertEqual(self.extract(text, 'ordinary'), (text, 1, 4))

    def test_single_letter_and_generic_named_return_types(self):
        for annotation in ('T', 'Promise<T>', 'ReadonlyArray<T>', 'T | null'):
            text = f'export function identity<T>(arg: T): {annotation} {{\n  return arg;\n}}'
            with self.subTest(annotation=annotation):
                self.assertEqual(self.extract(text, 'identity'), (text, 1, 3))

    def test_ambiguous_inline_return_type_uses_existing_window_fallback(self):
        text = 'export function make(): { ok: boolean } {\n  return { ok: true };\n}'
        self.assertEqual(self.extract(text, 'make'), (None, -1, -1))
        block, start, end = self.extractor.build_line_window(text, 0)
        self.assertIn('return { ok: true }', block)
        self.assertLessEqual(end - start + 1, 7)

    def test_overload_does_not_capture_next_implementation(self):
        text = '''export function select(arg: string): string;
export function select(arg: string | number) {
  return String(arg);
}
'''
        self.assertEqual(self.extractor.extract_enclosing_block(text, 0, 'select'), (None, -1, -1))

    def test_regex_literal_signature_is_not_claimed_precise(self):
        text = 'export function matches(pattern = /[(){}]/) {\n  return pattern.test("x");\n}'
        self.assertEqual(self.extract(text, 'matches'), (None, -1, -1))

    def test_line_bound_and_requested_anchor_are_preserved(self):
        text = 'export function large() {\n' + '  use();\n' * 220 + '  target();\n}'
        block, start, end = self.extract(text, 'large', max_block_lines=20)
        self.assertLessEqual(end - start + 1, 21)
        self.assertNotIn('target', block)
        self.assertEqual(self.extract(text, 'target', max_block_lines=20), (None, -1, -1))

    def test_simple_javascript_python_and_rust_are_unchanged(self):
        samples = [
            ('function plain() {\n  return 1;\n}', 'plain'),
            ('def plain(value):\n    return value\n', 'plain'),
            ('pub fn plain() {\n  consume();\n}', 'plain'),
        ]
        for text, symbol in samples:
            with self.subTest(text=text):
                block, start, end = self.extract(text, symbol)
                self.assertEqual(block, text.rstrip('\n'))
                self.assertEqual((start, end), (1, len(text.splitlines())))


if __name__ == '__main__':
    unittest.main()
