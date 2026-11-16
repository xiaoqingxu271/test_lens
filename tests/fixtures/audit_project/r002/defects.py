def simple_deref():
    x = None
    return x.value


def guarded_if():
    x = None
    if x is not None:
        return x.value
    return None


def truthy_guard():
    x = None
    if x:
        return x.value
    return None


def falsy_guard():
    x = None
    if not x:
        return x.value
    return None


def reassigned():
    x = None
    x = object()
    return x.value


def guarded_return():
    x = None
    if x is None:
        return None
    return x.value


def branch_assign(flag):
    x = None
    if flag:
        x = object()
    return x.value


def pass_through():
    x = None
    consume(x)
    return 0


def attr_store():
    x = None
    x.value = 1


def subscript():
    x = None
    return x[0]


def called():
    x = None
    return x()


def guarded_assert():
    x = None
    assert x is not None
    return x.value


def guarded_else(flag):
    x = None
    if x is None:
        x = object()
    return x.value


def conditional_expr():
    x = None
    return x.value if x else None


def safe_conditional_expr():
    x = None
    return x.value if x is not None else None


def conditional_none():
    x = None
    return x.value if x is None else None


def bool_and():
    x = None
    return x is None and x.value


def bool_or():
    x = None
    return x is None or x.value


def cross_function():
    x = None

    def inner():
        return x.value

    return inner
