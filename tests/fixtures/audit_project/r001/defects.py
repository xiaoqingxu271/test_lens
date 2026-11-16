def list_default(items=[]):
    return items


def dict_default(options={}):
    return options


def set_default(values=set()):
    return values


def bytearray_default(buffer=bytearray()):
    return buffer


def list_constructor(items=list()):
    return items


def tuple_default(pair=([],)):
    return pair


def comprehension_default(values=[item for item in range(3)]):
    return values


def safe_none(value=None):
    return value


def safe_tuple(value=()):
    return value


def safe_factory(value=field(default_factory=list)):
    return value
