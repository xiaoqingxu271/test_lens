from app import classify


def test_classify_strong():
    assert classify(1) == 1
    assert classify(0) == 0
    assert classify(-1) == -1
