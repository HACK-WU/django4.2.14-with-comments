from django.utils.itercompat import is_iterable


def make_hashable(value):
    """
    Attempt to make value hashable or raise a TypeError if it fails.

    The returned value should generate the same hash for equal values.

    1、若输入为字典，将其键值对排序后递归转换为元组。
    2、尝试直接对输入值进行哈希运算。
    3、若尝试失败且输入为可迭代类型，则递归转换其元素为可哈希类型并组成元组。
    4、若输入既不可哈希又不可迭代，则抛出TypeError。
    5、返回转换后的可哈希值。

    """
    if isinstance(value, dict):
        return tuple(
            [
                (key, make_hashable(nested_value))
                for key, nested_value in sorted(value.items())
            ]
        )
    # Try hash to avoid converting a hashable iterable (e.g. string, frozenset)
    # to a tuple.
    try:
        hash(value)
    except TypeError:
        if is_iterable(value):
            return tuple(map(make_hashable, value))
        # Non-hashable, non-iterable.
        raise
    return value
