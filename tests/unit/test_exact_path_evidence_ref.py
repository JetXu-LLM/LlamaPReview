"""Missing path prefixes may not erase evidence or invent new observations."""
import copy
import unittest

from lambdas.LlamaPReviewPipeline.review.presentation import compile_presentation_v1
from lambdas.LlamaPReviewPipeline.review.presentation_projection import _CompileState
from tests.unit.test_presentation_contract import (
    HEAD, PR_DETAILS, context_meta, finding, presentation,
)


class ExactPathEvidenceRefTests(unittest.TestCase):
    def admit(self, refs, meta=None):
        state = _CompileState(meta if meta is not None else context_meta(), set())
        admitted = state.admit_refs(refs, location='$.required', allow_partial_required=True)
        return admitted, state

    def test_exact_available_path_recovers_prefix_and_deduplicates(self):
        result, state = self.admit(['src/app.py', 'path:src/app.py'])
        self.assertEqual(result, ['path:src/app.py'])
        self.assertIn('$.required:exact_bare_path_ref_restored', state.normalizations)
        self.assertIn('$.required:duplicate_ref_removed', state.normalizations)
        self.assertFalse(state.issues)

    def test_canonical_refs_do_not_change(self):
        result, state = self.admit(['path:src/app.py'])
        self.assertEqual(result, ['path:src/app.py'])
        self.assertFalse(state.normalizations)

    def test_no_case_basename_relative_slash_or_fuzzy_guess(self):
        for value in ['app.py', 'src/App.py', './src/app.py', '/src/app.py',
                      r'src\app.py', 'src/ap.py', 'src/missing.py']:
            with self.subTest(value=value):
                result, state = self.admit([value])
                self.assertIsNone(result)
                self.assertEqual(state.issues[0].code, 'evidence_ref_out_of_catalog')
                self.assertNotIn('$.required:exact_bare_path_ref_restored', state.normalizations)

    def test_catalog_location_hint_unavailable_or_ambiguous_path_not_promoted(self):
        for change in [
            {'outcome': 'error'}, {'outcome': 'unavailable'}, {'outcome': ''},
            {'coverage_type': 'directory_inventory'}, {'coverage_type': 'exact_path_state'},
            {'paths': ['src/app.py', 'src/other.py']}, {'paths': ['./src/app.py']},
        ]:
            meta = context_meta()
            meta['evidence_catalog'][0].update(change)
            with self.subTest(change=change):
                result, state = self.admit(['src/app.py'], meta)
                self.assertIsNone(result)
                self.assertNotIn('$.required:exact_bare_path_ref_restored', state.normalizations)

    def test_wrong_head_or_unverified_default_branch_search_not_promoted(self):
        for change in [
            {'source_ref': 'pr_head:' + 'b' * 40},
            {'source_ref': 'default_branch_search', 'head_reread_outcome': 'unavailable'},
        ]:
            meta = context_meta()
            meta['evidence_catalog'][0].update(change)
            with self.subTest(change=change):
                result, state = self.admit(['src/app.py'], meta)
                self.assertIsNone(result)
                self.assertNotIn('$.required:exact_bare_path_ref_restored', state.normalizations)
        meta = context_meta()
        meta['evidence_catalog'][0]['source_ref'] = 'pr_head:' + HEAD
        self.assertEqual(self.admit(['src/app.py'], meta)[0], ['path:src/app.py'])

    def test_unknown_secondary_ref_still_drops_only_that_item(self):
        p = presentation(verdict='blocking', findings=[
            finding(headline='A supported blocker'),
            finding(headline='An unsupported extra item', required=['src/missing.py']),
        ])
        result = compile_presentation_v1(p, pr_details=PR_DETAILS, context_meta=context_meta())
        self.assertTrue(result.publishable)
        self.assertEqual(len(result.presentation['findings']), 1)
        self.assertTrue(any(i.code == 'evidence_ref_out_of_catalog' for i in result.issues))

    def test_second_finding_is_preserved_without_altering_its_judgment(self):
        second = finding(headline='An independently supported defect', required=['src/app.py'])
        p = presentation(verdict='blocking', findings=[finding(), second])
        original = copy.deepcopy(p)
        result = compile_presentation_v1(p, pr_details=PR_DETAILS, context_meta=context_meta())
        self.assertTrue(result.publishable)
        self.assertEqual(len(result.presentation['findings']), 2)
        recovered = result.presentation['findings'][1]
        for field in ('headline', 'analysis', 'owner_action', 'priority', 'confidence'):
            self.assertEqual(recovered[field], second[field])
        self.assertEqual(recovered['required_evidence_refs'], ['path:src/app.py'])
        self.assertEqual(p, original)

    def test_unknown_last_deciding_basis_remains_nonpublishable(self):
        p = presentation(verdict='blocking', findings=[finding(required=['src/missing.py'])])
        result = compile_presentation_v1(p, pr_details=PR_DETAILS, context_meta=context_meta())
        self.assertFalse(result.publishable)


if __name__ == '__main__':
    unittest.main()
