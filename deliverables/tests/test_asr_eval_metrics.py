from eval.asr.evaluate import cer, normalize_text, percentile, term_present


def test_normalize_text_handles_spacing_case_and_punctuation():
    assert normalize_text(" YOLO v 8，USB！") == "yolov8usb"


def test_cer_ignores_punctuation_but_counts_wrong_character():
    assert cer("先验框机制是什么啊？", "先验框机制是什么啊") == 0.0
    assert cer("先验框", "先验狂") == 1 / 3


def test_term_presence_requires_canonical_abbreviation():
    assert term_present("USB", "USB 接口")
    assert not term_present("USB", "U B 八接口")


def test_percentile_uses_linear_interpolation():
    assert percentile([100.0, 200.0, 300.0], 0.5) == 200.0
    assert percentile([], 0.95) is None
