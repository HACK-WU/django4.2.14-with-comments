from asgiref.local import Local

from django.conf import settings as django_settings
from django.utils.functional import cached_property


class ConnectionProxy:
    """Proxy for accessing a connection object's attributes."""

    def __init__(self, connections, alias):
        self.__dict__["_connections"] = connections
        self.__dict__["_alias"] = alias

    def __getattr__(self, item):
        return getattr(self._connections[self._alias], item)

    def __setattr__(self, name, value):
        return setattr(self._connections[self._alias], name, value)

    def __delattr__(self, name):
        return delattr(self._connections[self._alias], name)

    def __contains__(self, key):
        return key in self._connections[self._alias]

    def __eq__(self, other):
        return self._connections[self._alias] == other


class ConnectionDoesNotExist(Exception):
    pass

class BaseConnectionHandler:
    """
    数据库连接处理基类。

    该类用于管理不同数据库连接的创建和访问。它提供了配置设置、创建连接和管理连接生命周期的方法。
    """
    # 指定设置的名称，子类需要覆盖
    settings_name = None
    # 定义一个用于处理连接不存在情况的异常类，子类需要覆盖
    exception_class = ConnectionDoesNotExist
    # 标记此连接是否为线程关键，影响连接的创建和管理方式
    thread_critical = False

    def __init__(self, settings=None):
        """
        初始化连接处理器。

        :param settings: 可选参数，用于初始化连接设置。
        """
        self._settings = settings
        # 使用线程本地存储来管理连接，以支持多线程环境
        self._connections = Local(self.thread_critical)

    @cached_property
    def settings(self):
        """
        配置并返回连接设置。

        :return: 配置后的连接设置。
        """
        self._settings = self.configure_settings(self._settings)
        return self._settings

    def configure_settings(self, settings):
        """
        根据提供的设置和全局设置配置连接设置。

        :param settings: 传入的连接设置。
        :return: 配置好的连接设置。
        """
        if settings is None:
            settings = getattr(django_settings, self.settings_name)
        return settings

    def create_connection(self, alias):
        """
        创建指定别名的数据库连接。

        子类必须实现此方法。

        :param alias: 连接的别名。
        :raise NotImplementedError: 当子类未实现此方法时抛出。
        """
        raise NotImplementedError("Subclasses must implement create_connection().")

    def __getitem__(self, alias):
        """
        根据别名获取数据库连接。

        :param alias: 连接的别名。
        :return: 对应的数据库连接对象。
        :raise ConnectionDoesNotExist: 当连接不存在时抛出。
        """
        try:
            return getattr(self._connections, alias)
        except AttributeError:
            if alias not in self.settings:
                raise self.exception_class(f"The connection '{alias}' doesn't exist.")
        conn = self.create_connection(alias)
        setattr(self._connections, alias, conn)
        return conn

    def __setitem__(self, key, value):
        """
        设置数据库连接。

        :param key: 连接的别名。
        :param value: 连接对象。
        """
        setattr(self._connections, key, value)

    def __delitem__(self, key):
        """
        删除指定的数据库连接。

        :param key: 连接的别名。
        """
        delattr(self._connections, key)

    def __iter__(self):
        """
        迭代所有连接别名。

        :return: 迭代器。
        """
        return iter(self.settings)

    def all(self, initialized_only=False):
        """
        获取所有（或已初始化的）数据库连接。

        :param initialized_only: 仅返回已初始化的连接（默认为False）。
        :return: 连接列表。
        """
        return [
            self[alias]
            for alias in self
            if not initialized_only or hasattr(self._connections, alias)
        ]

    def close_all(self):
        """
        关闭所有已初始化的数据库连接。
        """
        for conn in self.all(initialized_only=True):
            conn.close()

