def after_return():
    return 1
    value = 2
    print(value)


def after_raise():
    raise ValueError("x")
    value = 3


def guarded_if():
    if True:
        return 1
    value = 4
    return value


def in_branch(flag):
    if flag:
        return 1
        value = 5
    return 0


def in_try():
    try:
        return 1
        value = 6
    except Exception:
        return 0
