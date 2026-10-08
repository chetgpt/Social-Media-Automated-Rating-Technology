"""Offline regression tests for evidence-first artist classification."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from music_discovery_classification import (
    assess_classification, classification_template, normalize_classification,
)


AS_OF = "2026-08-31T12:00:00+00:00"
SOURCE = "https://artist.example.com/biography"
WORK = "https://www.tiktok.com/@artist/video/1234567890123456789"


def cited(status, rationale="Cited public research supports this conclusion."):
    return {"status": status, "rationale": rationale, "sources": [SOURCE]}


def new_artist():
    payload = classification_template()
    career = payload["career_stage"]
    career.update(claim="new_artist", assessed_on="2026-08-31",
                  rationale="Debut and prior-history review support an early-career act.", sources=[SOURCE])
    career["earliest_release"] = {"published_at": "2026-08-12", "basis": "documented_debut", "sources": [SOURCE]}
    career["prior_history"] = cited("none_found")
    career["career_context"] = cited("early_career")
    return payload


def emerging():
    payload = classification_template()
    payload["momentum"].update(claim="emerging", assessed_on="2026-08-31",
                               rationale="Repeat growth alongside multiple documented performances.", sources=[SOURCE])
    payload["momentum"]["corroboration"] = cited("broader_attention")
    return payload


def observation(value, observed_at="2026-08-30T12:00:00Z", url=WORK, metric="views"):
    return {"url": url, "observed_at": observed_at, "metrics": {metric: value}}


class ClassificationTests(unittest.TestCase):
    def test_missing_claims_stay_pending(self):
        result = assess_classification(None, as_of=AS_OF)
        self.assertEqual(result["career_stage"]["label"], "uncertain")
        self.assertEqual(result["momentum"]["status"], "pending")
        self.assertNotIn("listening_review", result)
        self.assertNotIn("talent_score", result)

    def test_template_is_normalized_and_independent(self):
        first = classification_template()
        self.assertEqual(normalize_classification(first), first)
        first["career_stage"]["sources"].append(SOURCE)
        self.assertEqual(classification_template()["career_stage"]["sources"], [])

    def test_documented_debut_and_prior_review_support_new_artist(self):
        result = assess_classification(new_artist(), as_of=AS_OF)
        self.assertEqual(result["career_stage"]["label"], "new_artist")
        self.assertEqual(result["career_stage"]["confidence"], "supported")

    def test_first_seen_is_not_a_debut(self):
        payload = new_artist()
        payload["career_stage"]["earliest_release"]["basis"] = "first_seen_in_dataset"
        result = assess_classification(payload, as_of=AS_OF)
        self.assertEqual(result["career_stage"]["label"], "uncertain")
        self.assertIn("documented_debut_not_dataset_first_seen", result["career_stage"]["missing_evidence"])

    def test_old_catalog_and_ongoing_career_do_not_become_new(self):
        payload = new_artist()
        payload["career_stage"]["earliest_release"]["published_at"] = "2005-01-01"
        payload["career_stage"]["career_context"] = cited("ongoing_career")
        payload["career_stage"]["prior_history"] = cited("prior_music_career")
        result = assess_classification(payload, as_of=AS_OF)
        self.assertEqual(result["career_stage"]["label"], "uncertain")
        self.assertIn("cited_early_career_context", result["career_stage"]["missing_evidence"])

    def test_no_hidden_calendar_cutoff(self):
        payload = new_artist()
        payload["career_stage"]["earliest_release"]["published_at"] = "2023-01-01"
        result = assess_classification(payload, as_of=AS_OF)
        self.assertEqual(result["career_stage"]["label"], "new_artist")

    def test_rename_does_not_reset_artist_career(self):
        payload = new_artist()
        payload["career_stage"]["prior_history"] = cited("name_change")
        self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["label"], "uncertain")
        payload["career_stage"]["claim"] = "new_project"
        self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["label"], "uncertain")

    def test_distinct_new_project_separate_from_new_artist(self):
        payload = new_artist()
        payload["career_stage"]["claim"] = "new_project"
        payload["career_stage"]["prior_history"] = cited("new_project")
        self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["label"], "new_project")

    def test_unreviewed_prior_history_is_pending_not_rejected(self):
        payload = new_artist()
        payload["career_stage"]["prior_history"] = {"status": "not_reviewed"}
        normalized = normalize_classification(payload)
        result = assess_classification(normalized, as_of=AS_OF)
        self.assertEqual(result["career_stage"]["status"], "pending")
        self.assertIn("cited_prior_history_review", result["career_stage"]["missing_evidence"])

    def test_fabricated_listening_fields_not_accepted(self):
        payload = classification_template()
        payload["listening_review"] = {"status": "reviewed"}
        with self.assertRaises(ValueError):
            normalize_classification(payload)

    def test_future_debut_is_pending(self):
        payload = new_artist()
        payload["career_stage"]["earliest_release"]["published_at"] = "2027-01-01"
        result = assess_classification(payload, as_of=AS_OF)
        self.assertIn("debut_in_future", result["career_stage"]["missing_evidence"])

    def test_assessment_requires_explicit_applicability(self):
        payload = new_artist()
        payload["career_stage"]["assessed_on"] = "2026-08-30"
        self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["status"], "pending")
        payload["career_stage"]["applies_through"] = "2026-09-02"
        self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["label"], "new_artist")

    def test_future_or_reversed_assessment_window_is_pending(self):
        for assessed_on, through in (("2026-09-01", None), ("2026-08-31", "2026-08-30")):
            payload = new_artist()
            payload["career_stage"].update(assessed_on=assessed_on, applies_through=through)
            self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["status"], "pending")

    def test_missing_citations_downgrade_claim_without_rejecting_dossier(self):
        payload = new_artist()
        payload["career_stage"]["sources"] = []
        self.assertEqual(normalize_classification(payload)["career_stage"]["claim"], "new_artist")
        self.assertEqual(assess_classification(payload, as_of=AS_OF)["career_stage"]["label"], "uncertain")

    def test_one_viral_snapshot_is_not_momentum(self):
        result = assess_classification(emerging(), as_of=AS_OF, observations=[observation(10000000)])
        self.assertEqual(result["momentum"]["label"], "uncertain")
        self.assertIn("positive_comparable_repeat_observations", result["momentum"]["missing_evidence"])

    def test_repeat_observations_plus_corroboration_support_momentum(self):
        result = assess_classification(emerging(), as_of=AS_OF,
                                       observations=[observation(100), observation(200, AS_OF)])
        self.assertEqual(result["momentum"]["label"], "emerging")
        self.assertEqual(result["metric_changes"][0]["absolute_change"], 100)
        self.assertEqual(result["metric_changes"][0]["percent_change"], 100)

    def test_single_work_growth_requires_broader_attention(self):
        payload = emerging()
        payload["momentum"]["corroboration"] = cited("single_work_only")
        result = assess_classification(payload, as_of=AS_OF, observations=[observation(100), observation(500, AS_OF)])
        self.assertEqual(result["momentum"]["label"], "uncertain")
        self.assertIn("cited_attention_beyond_one_work", result["momentum"]["missing_evidence"])

    def test_duplicate_copies_and_equivalent_timezone_are_not_growth(self):
        points = [observation(100), observation(100, "2026-08-30T19:00:00+07:00")]
        result = assess_classification(emerging(), as_of=AS_OF, observations=points)
        self.assertEqual(result["momentum"]["label"], "uncertain")
        self.assertEqual(result["metric_changes"][0]["observation_count"], 1)

    def test_conflicting_values_at_same_time_invalidate_series(self):
        points = [observation(100), observation(120), observation(200, AS_OF)]
        result = assess_classification(emerging(), as_of=AS_OF, observations=points)
        self.assertEqual(result["momentum"]["label"], "uncertain")
        self.assertEqual(result["metric_changes"][0]["status"], "conflicting_observations")

    def test_no_cross_resource_or_cross_metric_growth(self):
        for second in (observation(200, AS_OF, url=SOURCE), observation(200, AS_OF, metric="likes")):
            result = assess_classification(emerging(), as_of=AS_OF, observations=[observation(100), second])
            self.assertEqual(result["momentum"]["label"], "uncertain")

    def test_zero_baseline_is_present_but_percent_is_not_infinite(self):
        result = assess_classification(emerging(), as_of=AS_OF,
                                       observations=[observation(0), observation(100, AS_OF)])
        self.assertEqual(result["momentum"]["label"], "emerging")
        self.assertEqual(result["metric_changes"][0]["from_value"], 0)
        self.assertIsNone(result["metric_changes"][0]["percent_change"])

    def test_negative_flat_missing_future_and_malformed_metrics_do_not_support_growth(self):
        for second in (observation(90, AS_OF), observation(100, AS_OF), observation(None, AS_OF),
                       observation(-1, AS_OF), observation(float("nan"), AS_OF),
                       observation(200, "2027-01-01T00:00:00Z"), observation(200, "unknown")):
            result = assess_classification(emerging(), as_of=AS_OF, observations=[observation(100), second])
            self.assertEqual(result["momentum"]["label"], "uncertain")

    def test_citations_allow_spotify_but_never_credentials(self):
        payload = new_artist()
        payload["career_stage"]["sources"] = ["https://OPEN.SPOTIFY.COM:443/artist/123#bio"]
        self.assertEqual(normalize_classification(payload)["career_stage"]["sources"], ["https://open.spotify.com/artist/123"])
        for url in ("https://127.0.0.1/bio", "https://artist.local/bio", "https://artist.example.com/?token=secret", "https://user:pass@artist.example.com/bio"):
            payload["career_stage"]["sources"] = [url]
            with self.assertRaises(ValueError):
                normalize_classification(payload)

    def test_assessment_is_deterministic_and_does_not_mutate_inputs(self):
        payload, points = emerging(), [observation(100), observation(200, AS_OF)]
        before = deepcopy((payload, points))
        self.assertEqual(assess_classification(payload, as_of=AS_OF, observations=points),
                         assess_classification(payload, as_of=datetime(2026, 8, 31, 12, tzinfo=timezone.utc), observations=points))
        self.assertEqual((payload, points), before)


if __name__ == "__main__":
    unittest.main()
