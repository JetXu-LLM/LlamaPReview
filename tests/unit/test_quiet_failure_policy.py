"""Failed/cancelled free reviews never create new owner notifications."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.unit import test_publication_recovery as base
from tests.unit import test_review_unavailable_notice as failed
from lambdas.LlamaPReviewPipeline import pipeline_publication, persistence
from lambdas.LlamaPReviewPipeline.errors import PublicationOutcomeUnknown, PublicationStateConflict
from lambdas.LlamaPReviewPipeline.review.publication import mark_dispatching
from lambdas.LlamaPReviewPipeline.review.publication_candidate import build_candidate, persist_prepared_intent
from lambdas.LlamaPReviewPipeline.review.publish import prepare_main_comment_publication


class QuietFailurePolicyTests(unittest.TestCase):
    def setUp(self):
        self.h = base.PublicationTransactionTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.guard = patch('socket.socket.connect', side_effect=AssertionError('offline test'))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def prepare(self, mode, *, state='prepared'):
        self.h._put_item()
        claim = self.h._claim()
        if mode == 'normal':
            prepared = base._prepared()
        else:
            prepared = prepare_main_comment_publication(
                'Historical unsuccessful review notice', head_sha=base.HEAD,
                review_mode=mode,
                publication_kind='lifecycle_cancellation' if mode == 'cancelled' else 'ordinary_review',
                required_disposition='merged_same_head' if mode == 'cancelled' else 'open_same_head',
            )
            prepared.artifact.update(review_generation_status='cancelled' if mode == 'cancelled' else 'failed',
                                     review_failure_kind='presentation_invalid',review_failure_stage='final_presentation')
        candidate = build_candidate(
            prepared, repo='owner/repo',pr_number=7,run_id='run-7',phase='review',
            owner_event_id=base.EVENT,owner_request_id='request-1',publication_generation_attempt=1,
            preflight_completed_at=base.NOW.isoformat(),generation_runtime_identity={'phase':'review'},
            terminal_attributes={'review_generation_status':'complete' if mode=='normal' else mode,
                                 'review_failure_kind':'presentation_invalid',**base._complete_accounting()},
            publication_key='a'*32,
        )
        with patch.object(persistence,'get_s3_client',return_value=self.h.s3):
            intent=persist_prepared_intent(candidate,expected_status='CONTEXT_READY',phase_claim=claim,table=self.h.table)
            if state=='dispatching':
                mark_dispatching(repo='owner/repo',pr_number=7,expected_status='CONTEXT_READY',intent=intent,phase_claim=claim,table=self.h.table)
        recovery=self.h._claim(request='request-2')
        current=persistence.get_item('owner/repo',7,table=self.h.table)
        context=pipeline_publication.PublicationContext(
            repo='owner/repo',pr_number=7,head_sha=base.HEAD,expected_status='CONTEXT_READY',phase='review',
            run_id='run-7',generation_attempt=2,runtime_identity={'phase':'review'},phase_claim=recovery,dry_run=False,
        )
        runtime=SimpleNamespace(get_repository=Mock(return_value=self.h.repo),get_pr_head_snapshot=Mock(return_value={
            'head_sha':base.HEAD,'state':'open','merged':False,'locked':False}))
        return prepared,current,context,runtime

    def recover(self,current,context,runtime):
        with patch.object(persistence,'get_s3_client',return_value=self.h.s3),patch.object(pipeline_publication,'fetch_pr_details',return_value=({},[])):
            return pipeline_publication.recover_pending(current,context=context,runtime=runtime,deadline=None,table=self.h.table)

    def test_prepared_failed_and_cancelled_suppress_without_any_github_read_or_write(self):
        for mode in ('failed','cancelled'):
            with self.subTest(mode=mode):
                self.h.setUp()
                _,current,context,runtime=self.prepare(mode)
                prior=deepcopy(current['publication_intent'])
                self.assertTrue(self.recover(current,context,runtime))
                runtime.get_repository.assert_not_called()
                runtime.get_pr_head_snapshot.assert_not_called()
                stored=persistence.get_item('owner/repo',7,table=self.h.table)
                self.assertEqual(stored['status'],'ERROR' if mode=='failed' else 'SUPERSEDED')
                self.assertEqual(stored['review_generation_status'],mode)
                self.assertEqual(stored['publication_status'],'suppressed')
                self.assertIs(stored['publication_post_started'],False)
                self.assertEqual(stored['publication_intent']['publication_key'],prior['publication_key'])
                self.assertNotIn('publication_receipt',stored)
                self.assertFalse(stored['quality_scoreable'])
                self.assertEqual(stored['deepseek_usage_total'],base._complete_accounting()['deepseek_usage_total'])
                self.assertEqual(self.h.pull.create_count,0)

    def test_private_suppression_lost_compare_and_swap_never_posts_or_claims_success(self):
        _,current,context,runtime=self.prepare('failed')
        with patch.object(persistence, 'update_status', return_value=False) as update:
            with self.assertRaises(PublicationStateConflict):
                self.recover(current,context,runtime)
        runtime.get_repository.assert_not_called()
        self.assertEqual(self.h.pull.create_count,0)
        self.assertEqual(update.call_args.kwargs['expected_missing_attributes'],('publication_receipt',))
        self.assertIn('publication_intent',update.call_args.kwargs['expected_attributes'])
        self.assertEqual(update.call_args.kwargs['phase_claim'],context.phase_claim)

    def test_normal_prepared_review_still_publishes_once(self):
        _,current,context,runtime=self.prepare('normal')
        self.assertTrue(self.recover(current,context,runtime))
        self.assertEqual(self.h.pull.create_count,1)
        self.assertEqual(persistence.get_item('owner/repo',7,table=self.h.table)['status'],'PROCESSED')

    def test_dispatching_failed_notice_only_adopts_existing_historical_review(self):
        prepared,current,context,runtime=self.prepare('failed',state='dispatching')
        historical=base._review(prepared)
        self.h.pull.review_pages=[[historical]]
        original_body=historical.body
        self.assertTrue(self.recover(current,context,runtime))
        self.assertEqual(self.h.pull.create_count,0)
        self.assertEqual(historical.body,original_body)
        stored=persistence.get_item('owner/repo',7,table=self.h.table)
        self.assertEqual(stored['publication_receipt']['outcome'],'adopted')
        self.assertEqual(stored['review_generation_status'],'failed')

    def test_dispatching_outcome_unknown_stays_unknown_without_second_post(self):
        _,current,context,runtime=self.prepare('failed',state='dispatching')
        with self.assertRaises(PublicationOutcomeUnknown):
            self.recover(current,context,runtime)
        self.assertEqual(self.h.pull.create_count,0)
        stored=persistence.get_item('owner/repo',7,table=self.h.table)
        self.assertNotEqual(stored.get('publication_status'),'suppressed')
        self.assertNotIn('publication_receipt',stored)

    def test_all_terminal_final_failures_remain_private_with_accounting(self):
        for dry_run in (False,True):
            for complete in (False,True):
                with self.subTest(dry_run=dry_run,complete=complete),patch.object(failed.orchestrator.pipeline_admission,'assert_current_head',return_value=failed.HEAD),patch.object(failed.orchestrator.persistence,'store_review_failure',return_value=True) as store,patch.object(failed.orchestrator.pipeline_publication,'commit_prepared') as post:
                    self.assertTrue(failed._call(dry_run=dry_run,usage=failed._usage(completed=complete)))
                    post.assert_not_called()
                    self.assertEqual(store.call_args.kwargs['extra_attrs']['deepseek_usage_total'],failed._usage(completed=complete)['deepseek_usage_total'])

if __name__=='__main__':unittest.main()
