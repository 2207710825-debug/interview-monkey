from scripts.check_asr_regression import compare_summaries


def test_asr_gate_allows_equal_or_improved_candidate():
    baseline = {"macro_cer": 0.031, "hotword_recall": 1.0}
    candidate = {"macro_cer": 0.030, "hotword_recall": 1.0}
    assert compare_summaries(candidate, baseline) == []


def test_asr_gate_rejects_cer_or_hotword_recall_regression():
    baseline = {"macro_cer": 0.031, "hotword_recall": 1.0}
    candidate = {"macro_cer": 0.032, "hotword_recall": 0.98}
    failures = compare_summaries(candidate, baseline)
    assert len(failures) == 2
