def bare_pass():
    try:
        do()
    except:
        pass


def bare_log():
    try:
        do()
    except:
        log()


def bare_reraise():
    try:
        do()
    except:
        raise


def bare_ellipsis():
    try:
        do()
    except:
        ...


def narrow():
    try:
        do()
    except Exception:
        pass
