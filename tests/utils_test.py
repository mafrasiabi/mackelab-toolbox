def int_if_close_test():
    xint = 5
    xfloat = 5.1
    xnpfloat = np.float32(5.1)
    xeps = 5 + np.finfo(float).eps * 50
    arrint = np.array([xint, xint])
    arrmixed = np.array([xint, xfloat, xeps])

    assert(ml.utils.int_if_close(xint) is xint)

    assert(np.rint(xint).astype(np.int) == xint)

    assert(ml.utils.int_if_close(xfloat) is xfloat)

    assert(ml.utils.int_if_close(xnpfloat) is xnpfloat)

    assert(ml.utils.int_if_close(xeps) == xint)

    assert(ml.utils.int_if_close(arrint) is arrint)

    assert(ml.utils.int_if_close(arrmixed) is arrmixed)
