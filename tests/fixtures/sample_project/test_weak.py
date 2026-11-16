from app import classify


def test_classify_weak():
    classify(1)
    classify(0)
    classify(-1)
