import _thread
import copy
import datetime
import logging
import threading
import time
import warnings
from collections import deque
from contextlib import contextmanager

from django.db.backends.utils import debug_transaction

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, DatabaseError, NotSupportedError
from django.db.backends import utils
from django.db.backends.base.validation import BaseDatabaseValidation
from django.db.backends.signals import connection_created
from django.db.transaction import TransactionManagementError
from django.db.utils import DatabaseErrorWrapper
from django.utils.asyncio import async_unsafe
from django.utils.functional import cached_property

NO_DB_ALIAS = "__no_db__"
RAN_DB_VERSION_CHECK = set()

logger = logging.getLogger("django.db.backends.base")


# RemovedInDjango50Warning
def timezone_constructor(tzname):
    if settings.USE_DEPRECATED_PYTZ:
        import pytz

        return pytz.timezone(tzname)
    return zoneinfo.ZoneInfo(tzname)


class BaseDatabaseWrapper:
    """Represent a database connection."""

    # Mapping of Field objects to their column types.
    data_types = {}
    # Mapping of Field objects to their SQL suffix such as AUTOINCREMENT.
    data_types_suffix = {}
    # Mapping of Field objects to their SQL for CHECK constraints.
    data_type_check_constraints = {}
    ops = None
    vendor = "unknown"
    display_name = "unknown"
    SchemaEditorClass = None
    # Classes instantiated in __init__().
    client_class = None
    creation_class = None
    features_class = None
    introspection_class = None
    ops_class = None
    validation_class = BaseDatabaseValidation

    queries_limit = 9000

    def __init__(self, settings_dict, alias=DEFAULT_DB_ALIAS):
        """
        初始化数据库连接。

        :param settings_dict: 包含数据库连接信息的字典，如数据库名、用户名等。
        :param alias: 数据库别名，默认为DEFAULT_DB_ALIAS。
        """
        # 连接相关属性
        self.connection = None
        # `settings_dict` 应该是一个包含数据库配置信息的字典，例如NAME、USER等键。
        self.settings_dict = settings_dict
        self.alias = alias
        # 在调试模式下或显式启用时记录查询日志
        self.queries_log = deque(maxlen=self.queries_limit)
        self.force_debug_cursor = False

        # 事务相关属性
        # 跟踪连接是否处于自动提交模式。根据PEP 249，默认不是。
        self.autocommit = False
        # 跟踪连接是否由 'atomic' 管理的事务中。
        self.in_atomic_block = False
        # 递增以生成唯一的保存点ID。
        self.savepoint_state = 0
        # 由 'atomic' 创建的保存点列表。
        self.savepoint_ids = []
        # 活动的 'atomic' 块堆栈。
        self.atomic_blocks = []
        # 跟踪最外层的 'atomic' 块在退出时是否应该提交，
        # 即进入时是否启用了自动提交。
        self.commit_on_exit = True
        # 跟踪由于内部块中的异常而需要回滚到下一个可用保存点的情况。
        self.needs_rollback = False
        self.rollback_exc = None

        # 连接终止相关属性
        self.close_at = None
        self.closed_in_transaction = False
        self.errors_occurred = False
        self.health_check_enabled = False
        self.health_check_done = False

        # 线程安全相关属性
        self._thread_sharing_lock = threading.Lock()
        self._thread_sharing_count = 0
        self._thread_ident = _thread.get_ident()

        # 当事务提交时运行的无参数函数列表。
        # 每个条目是一个 (sids, func, robust) 元组，其中sids是在注册此函数时活动的保存点ID集合，
        # robust指定是否允许函数失败。
        self.run_on_commit = []

        # 下次调用set_autocommit(True)时是否应运行on-commit钩子？
        self.run_commit_hooks_on_set_autocommit_on = False

        # 在execute()/executemany()调用周围被调用的包装器堆栈。
        # 每个条目是一个接受五个参数的函数：execute, sql, params, many, 和context。
        # 函数负责调用execute(sql, params, many, context)。
        self.execute_wrappers = []

        # 初始化数据库客户端及相关类
        self.client = self.client_class(self)x
        self.creation = self.creation_class(self)
        self.features = self.features_class(self)
        self.introspection = self.introspection_class(self)
        self.ops = self.ops_class(self)
        self.validation = self.validation_class(self)

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__} "
            f"vendor={self.vendor!r} alias={self.alias!r}>"
        )

    def ensure_timezone(self):
        """
        Ensure the connection's timezone is set to `self.timezone_name` and
        return whether it changed or not.
        """
        return False

    @cached_property
    def timezone(self):
        """
        Return a tzinfo of the database connection time zone.

        This is only used when time zone support is enabled. When a datetime is
        read from the database, it is always returned in this time zone.

        When the database backend supports time zones, it doesn't matter which
        time zone Django uses, as long as aware datetimes are used everywhere.
        Other users connecting to the database can choose their own time zone.

        When the database backend doesn't support time zones, the time zone
        Django uses may be constrained by the requirements of other users of
        the database.
        """
        if not settings.USE_TZ:
            return None
        elif self.settings_dict["TIME_ZONE"] is None:
            return datetime.timezone.utc
        else:
            return timezone_constructor(self.settings_dict["TIME_ZONE"])

    @cached_property
    def timezone_name(self):
        """
        Name of the time zone of the database connection.
        """
        if not settings.USE_TZ:
            return settings.TIME_ZONE
        elif self.settings_dict["TIME_ZONE"] is None:
            return "UTC"
        else:
            return self.settings_dict["TIME_ZONE"]

    @property
    def queries_logged(self):
        return self.force_debug_cursor or settings.DEBUG

    @property
    def queries(self):
        if len(self.queries_log) == self.queries_log.maxlen:
            warnings.warn(
                "Limit for query logging exceeded, only the last {} queries "
                "will be returned.".format(self.queries_log.maxlen)
            )
        return list(self.queries_log)

    def get_database_version(self):
        """Return a tuple of the database's version."""
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require a get_database_version() "
            "method."
        )

    def check_database_version_supported(self):
        """
        Raise an error if the database version isn't supported by this
        version of Django.
        """
        if (
                self.features.minimum_database_version is not None
                and self.get_database_version() < self.features.minimum_database_version
        ):
            db_version = ".".join(map(str, self.get_database_version()))
            min_db_version = ".".join(map(str, self.features.minimum_database_version))
            raise NotSupportedError(
                f"{self.display_name} {min_db_version} or later is required "
                f"(found {db_version})."
            )

    # ##### Backend-specific methods for creating connections and cursors #####

    def get_connection_params(self):
        """Return a dict of parameters suitable for get_new_connection."""
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require a get_connection_params() "
            "method"
        )

    def get_new_connection(self, conn_params):
        """Open a connection to the database."""
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require a get_new_connection() "
            "method"
        )

    def init_connection_state(self):
        """Initialize the database connection settings."""
        global RAN_DB_VERSION_CHECK
        if self.alias not in RAN_DB_VERSION_CHECK:
            self.check_database_version_supported()
            RAN_DB_VERSION_CHECK.add(self.alias)

    def create_cursor(self, name=None):
        """Create a cursor. Assume that a connection is established."""
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require a create_cursor() method"
        )

    # ##### Backend-specific methods for creating connections #####

    @async_unsafe
    def connect(self):
        """Connect to the database. Assume that the connection is closed."""
        # Check for invalid configurations.
        self.check_settings()
        # In case the previous connection was closed while in an atomic block
        self.in_atomic_block = False
        self.savepoint_ids = []
        self.atomic_blocks = []
        self.needs_rollback = False
        # Reset parameters defining when to close/health-check the connection.
        self.health_check_enabled = self.settings_dict["CONN_HEALTH_CHECKS"]
        max_age = self.settings_dict["CONN_MAX_AGE"]
        self.close_at = None if max_age is None else time.monotonic() + max_age
        self.closed_in_transaction = False
        self.errors_occurred = False
        # New connections are healthy.
        self.health_check_done = True
        # Establish the connection
        conn_params = self.get_connection_params()
        self.connection = self.get_new_connection(conn_params)
        self.set_autocommit(self.settings_dict["AUTOCOMMIT"])
        self.init_connection_state()
        connection_created.send(sender=self.__class__, connection=self)

        self.run_on_commit = []

    def check_settings(self):
        if self.settings_dict["TIME_ZONE"] is not None and not settings.USE_TZ:
            raise ImproperlyConfigured(
                "Connection '%s' cannot set TIME_ZONE because USE_TZ is False."
                % self.alias
            )

    @async_unsafe
    def ensure_connection(self):
        """Guarantee that a connection to the database is established."""
        if self.connection is None:
            with self.wrap_database_errors:
                self.connect()

    # ##### Backend-specific wrappers for PEP-249 connection methods #####

    def _prepare_cursor(self, cursor):
        """
        Validate the connection is usable and perform database cursor wrapping.
        """
        self.validate_thread_sharing()
        # 如果设置了DEBUG=True,那将返回一个CursorDebugWrapper对象
        if self.queries_logged:
            wrapped_cursor = self.make_debug_cursor(cursor)
        else:
            wrapped_cursor = self.make_cursor(cursor)
        return wrapped_cursor

    def _cursor(self, name=None):
        """
        创建并准备游标对象。

        该方法负责在执行数据库操作前，确保数据库连接健康，并创建合适的游标。
        它包含几个关键步骤：
        1. 如果健康检查失败，则关闭连接。
        2. 确保数据库连接已建立。
        3. 在数据库错误的上下文中执行，捕获并处理数据库错误。
        4. 创建游标，并对其进行必要的准备。

        参数:
        - name: 可选参数，用于标识游标的名字。

        返回:
        - 返回一个准备好的游标对象，用于执行SQL命令。
        """
        # 关闭健康检查失败的连接，确保数据库连接正常
        self.close_if_health_check_failed()
        # 确保数据库连接已建立
        self.ensure_connection()
        # 在数据库错误处理的上下文中执行游标创建和准备操作
        with self.wrap_database_errors:
            # 创建并返回准备好的游标
            return self._prepare_cursor(self.create_cursor(name))

    def _commit(self):
        if self.connection is not None:
            with debug_transaction(self, "COMMIT"), self.wrap_database_errors:
                return self.connection.commit()

    def _rollback(self):
        if self.connection is not None:
            with debug_transaction(self, "ROLLBACK"), self.wrap_database_errors:
                return self.connection.rollback()

    def _close(self):
        if self.connection is not None:
            with self.wrap_database_errors:
                return self.connection.close()

    # ##### Generic wrappers for PEP-249 connection methods #####

    @async_unsafe
    def cursor(self):
        """Create a cursor, opening a connection if necessary."""
        return self._cursor()

    @async_unsafe
    def commit(self):
        """Commit a transaction and reset the dirty flag."""
        self.validate_thread_sharing()
        self.validate_no_atomic_block()
        self._commit()
        # A successful commit means that the database connection works.
        self.errors_occurred = False
        self.run_commit_hooks_on_set_autocommit_on = True

    @async_unsafe
    def rollback(self):
        """Roll back a transaction and reset the dirty flag."""
        self.validate_thread_sharing()
        self.validate_no_atomic_block()
        self._rollback()
        # A successful rollback means that the database connection works.
        self.errors_occurred = False
        self.needs_rollback = False
        self.run_on_commit = []

    @async_unsafe
    def close(self):
        """Close the connection to the database."""
        self.validate_thread_sharing()
        self.run_on_commit = []

        # Don't call validate_no_atomic_block() to avoid making it difficult
        # to get rid of a connection in an invalid state. The next connect()
        # will reset the transaction state anyway.
        if self.closed_in_transaction or self.connection is None:
            return
        try:
            self._close()
        finally:
            if self.in_atomic_block:
                self.closed_in_transaction = True
                self.needs_rollback = True
            else:
                self.connection = None

    # ##### Backend-specific savepoint management methods #####

    def _savepoint(self, sid):
        with self.cursor() as cursor:
            cursor.execute(self.ops.savepoint_create_sql(sid))

    def _savepoint_rollback(self, sid):
        with self.cursor() as cursor:
            cursor.execute(self.ops.savepoint_rollback_sql(sid))

    def _savepoint_commit(self, sid):
        with self.cursor() as cursor:
            cursor.execute(self.ops.savepoint_commit_sql(sid))

    def _savepoint_allowed(self):
        # Savepoints cannot be created outside a transaction
        return self.features.uses_savepoints and not self.get_autocommit()

    # ##### Generic savepoint management methods #####

    @async_unsafe
    def savepoint(self):
        """
        Create a savepoint inside the current transaction. Return an
        identifier for the savepoint that will be used for the subsequent
        rollback or commit. Do nothing if savepoints are not supported.
        """
        if not self._savepoint_allowed():
            return

        thread_ident = _thread.get_ident()
        tid = str(thread_ident).replace("-", "")

        self.savepoint_state += 1
        sid = "s%s_x%d" % (tid, self.savepoint_state)

        self.validate_thread_sharing()
        self._savepoint(sid)

        return sid

    @async_unsafe
    def savepoint_rollback(self, sid):
        """
        Roll back to a savepoint. Do nothing if savepoints are not supported.
        """
        if not self._savepoint_allowed():
            return

        self.validate_thread_sharing()
        self._savepoint_rollback(sid)

        # Remove any callbacks registered while this savepoint was active.
        self.run_on_commit = [
            (sids, func, robust)
            for (sids, func, robust) in self.run_on_commit
            if sid not in sids
        ]

    @async_unsafe
    def savepoint_commit(self, sid):
        """
        Release a savepoint. Do nothing if savepoints are not supported.
        """
        if not self._savepoint_allowed():
            return

        self.validate_thread_sharing()
        self._savepoint_commit(sid)

    @async_unsafe
    def clean_savepoints(self):
        """
        Reset the counter used to generate unique savepoint ids in this thread.
        """
        self.savepoint_state = 0

    # ##### Backend-specific transaction management methods #####

    def _set_autocommit(self, autocommit):
        """
        Backend-specific implementation to enable or disable autocommit.
        """
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require a _set_autocommit() method"
        )

    # ##### Generic transaction management methods #####

    def get_autocommit(self):
        """Get the autocommit state."""
        self.ensure_connection()
        return self.autocommit

    def set_autocommit(
            self, autocommit, force_begin_transaction_with_broken_autocommit=False
    ):
        """
        Enable or disable autocommit.

        The usual way to start a transaction is to turn autocommit off.
        SQLite does not properly start a transaction when disabling
        autocommit. To avoid this buggy behavior and to actually enter a new
        transaction, an explicit BEGIN is required. Using
        force_begin_transaction_with_broken_autocommit=True will issue an
        explicit BEGIN with SQLite. This option will be ignored for other
        backends.
        """
        self.validate_no_atomic_block()
        self.close_if_health_check_failed()
        self.ensure_connection()

        start_transaction_under_autocommit = (
                force_begin_transaction_with_broken_autocommit
                and not autocommit
                and hasattr(self, "_start_transaction_under_autocommit")
        )

        if start_transaction_under_autocommit:
            self._start_transaction_under_autocommit()
        elif autocommit:
            self._set_autocommit(autocommit)
        else:
            with debug_transaction(self, "BEGIN"):
                self._set_autocommit(autocommit)
        self.autocommit = autocommit

        if autocommit and self.run_commit_hooks_on_set_autocommit_on:
            self.run_and_clear_commit_hooks()
            self.run_commit_hooks_on_set_autocommit_on = False

    def get_rollback(self):
        """Get the "needs rollback" flag -- for *advanced use* only."""
        if not self.in_atomic_block:
            raise TransactionManagementError(
                "The rollback flag doesn't work outside of an 'atomic' block."
            )
        return self.needs_rollback

    def set_rollback(self, rollback):
        """
        Set or unset the "needs rollback" flag -- for *advanced use* only.
        """
        if not self.in_atomic_block:
            raise TransactionManagementError(
                "The rollback flag doesn't work outside of an 'atomic' block."
            )
        self.needs_rollback = rollback

    def validate_no_atomic_block(self):
        """Raise an error if an atomic block is active."""
        if self.in_atomic_block:
            raise TransactionManagementError(
                "This is forbidden when an 'atomic' block is active."
            )

    def validate_no_broken_transaction(self):
        if self.needs_rollback:
            raise TransactionManagementError(
                "An error occurred in the current transaction. You can't "
                "execute queries until the end of the 'atomic' block."
            ) from self.rollback_exc

    # ##### Foreign key constraints checks handling #####

    @contextmanager
    def constraint_checks_disabled(self):
        """
        Disable foreign key constraint checking.
        """
        disabled = self.disable_constraint_checking()
        try:
            yield
        finally:
            if disabled:
                self.enable_constraint_checking()

    def disable_constraint_checking(self):
        """
        Backends can implement as needed to temporarily disable foreign key
        constraint checking. Should return True if the constraints were
        disabled and will need to be reenabled.
        """
        return False

    def enable_constraint_checking(self):
        """
        Backends can implement as needed to re-enable foreign key constraint
        checking.
        """
        pass

    def check_constraints(self, table_names=None):
        """
        Backends can override this method if they can apply constraint
        checking (e.g. via "SET CONSTRAINTS ALL IMMEDIATE"). Should raise an
        IntegrityError if any invalid foreign key references are encountered.
        """
        pass

    # ##### Connection termination handling #####

    def is_usable(self):
        """
        Test if the database connection is usable.

        This method may assume that self.connection is not None.

        Actual implementations should take care not to raise exceptions
        as that may prevent Django from recycling unusable connections.
        """
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require an is_usable() method"
        )

    def close_if_health_check_failed(self):
        """Close existing connection if it fails a health check."""
        if (
                self.connection is None
                or not self.health_check_enabled
                or self.health_check_done
        ):
            return

        if not self.is_usable():
            self.close()
        self.health_check_done = True

    def close_if_unusable_or_obsolete(self):
        """
        Close the current connection if unrecoverable errors have occurred
        or if it outlived its maximum age.
        """
        if self.connection is not None:
            self.health_check_done = False
            # If the application didn't restore the original autocommit setting,
            # don't take chances, drop the connection.
            if self.get_autocommit() != self.settings_dict["AUTOCOMMIT"]:
                self.close()
                return

            # If an exception other than DataError or IntegrityError occurred
            # since the last commit / rollback, check if the connection works.
            if self.errors_occurred:
                if self.is_usable():
                    self.errors_occurred = False
                    self.health_check_done = True
                else:
                    self.close()
                    return

            if self.close_at is not None and time.monotonic() >= self.close_at:
                self.close()
                return

    # ##### Thread safety handling #####

    @property
    def allow_thread_sharing(self):
        with self._thread_sharing_lock:
            return self._thread_sharing_count > 0

    def inc_thread_sharing(self):
        with self._thread_sharing_lock:
            self._thread_sharing_count += 1

    def dec_thread_sharing(self):
        with self._thread_sharing_lock:
            if self._thread_sharing_count <= 0:
                raise RuntimeError(
                    "Cannot decrement the thread sharing count below zero."
                )
            self._thread_sharing_count -= 1

    def validate_thread_sharing(self):
        """
        Validate that the connection isn't accessed by another thread than the
        one which originally created it, unless the connection was explicitly
        authorized to be shared between threads (via the `inc_thread_sharing()`
        method). Raise an exception if the validation fails.
        """
        if not (self.allow_thread_sharing or self._thread_ident == _thread.get_ident()):
            raise DatabaseError(
                "DatabaseWrapper objects created in a "
                "thread can only be used in that same thread. The object "
                "with alias '%s' was created in thread id %s and this is "
                "thread id %s." % (self.alias, self._thread_ident, _thread.get_ident())
            )

    # ##### Miscellaneous #####

    def prepare_database(self):
        """
        Hook to do any database check or preparation, generally called before
        migrating a project or an app.
        """
        pass

    @cached_property
    def wrap_database_errors(self):
        """
        Context manager and decorator that re-throws backend-specific database
        exceptions using Django's common wrappers.
        """
        return DatabaseErrorWrapper(self)

    def chunked_cursor(self):
        """
        Return a cursor that tries to avoid caching in the database (if
        supported by the database), otherwise return a regular cursor.
        """
        return self.cursor()

    def make_debug_cursor(self, cursor):
        """Create a cursor that logs all queries in self.queries_log."""
        return utils.CursorDebugWrapper(cursor, self)

    def make_cursor(self, cursor):
        """Create a cursor without debug logging."""
        return utils.CursorWrapper(cursor, self)

    @contextmanager
    def temporary_connection(self):
        """
        Context manager that ensures that a connection is established, and
        if it opened one, closes it to avoid leaving a dangling connection.
        This is useful for operations outside of the request-response cycle.

        经过conextmanager修饰的函数，可以使用with的方式进行执行，具有了上下文管理的功能
        Provide a cursor: with self.temporary_connection() as cursor: ...
        """
        must_close = self.connection is None
        try:
            with self.cursor() as cursor:
                yield cursor
        finally:
            if must_close:
                self.close()

    @contextmanager
    def _nodb_cursor(self):
        """
        Return a cursor from an alternative connection to be used when there is
        no need to access the main database, specifically for test db
        creation/deletion. This also prevents the production database from
        being exposed to potential child threads while (or after) the test
        database is destroyed. Refs #10868, #17786, #16969.
        """
        conn = self.__class__({**self.settings_dict, "NAME": None}, alias=NO_DB_ALIAS)
        try:
            with conn.cursor() as cursor:
                yield cursor
        finally:
            conn.close()

    def schema_editor(self, *args, **kwargs):
        """
        Return a new instance of this backend's SchemaEditor.
        """
        if self.SchemaEditorClass is None:
            raise NotImplementedError(
                "The SchemaEditorClass attribute of this database wrapper is still None"
            )
        return self.SchemaEditorClass(self, *args, **kwargs)

    def on_commit(self, func, robust=False):
        if not callable(func):
            raise TypeError("on_commit()'s callback must be a callable.")
        if self.in_atomic_block:
            # Transaction in progress; save for execution on commit.
            self.run_on_commit.append((set(self.savepoint_ids), func, robust))
        elif not self.get_autocommit():
            raise TransactionManagementError(
                "on_commit() cannot be used in manual transaction management"
            )
        else:
            # No transaction in progress and in autocommit mode; execute
            # immediately.
            if robust:
                try:
                    func()
                except Exception as e:
                    logger.error(
                        f"Error calling {func.__qualname__} in on_commit() (%s).",
                        e,
                        exc_info=True,
                    )
            else:
                func()

    def run_and_clear_commit_hooks(self):
        self.validate_no_atomic_block()
        current_run_on_commit = self.run_on_commit
        self.run_on_commit = []
        while current_run_on_commit:
            _, func, robust = current_run_on_commit.pop(0)
            if robust:
                try:
                    func()
                except Exception as e:
                    logger.error(
                        f"Error calling {func.__qualname__} in on_commit() during "
                        f"transaction (%s).",
                        e,
                        exc_info=True,
                    )
            else:
                func()

    @contextmanager
    def execute_wrapper(self, wrapper):
        """
        Return a context manager under which the wrapper is applied to suitable
        database query executions.
        """
        self.execute_wrappers.append(wrapper)
        try:
            yield
        finally:
            self.execute_wrappers.pop()

    def copy(self, alias=None):
        """
        Return a copy of this connection.

        For tests that require two connections to the same database.
        """
        settings_dict = copy.deepcopy(self.settings_dict)
        if alias is None:
            alias = self.alias
        return type(self)(settings_dict, alias)
