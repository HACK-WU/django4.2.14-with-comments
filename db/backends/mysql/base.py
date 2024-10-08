"""
MySQL database backend for Django.

Requires mysqlclient: https://pypi.org/project/mysqlclient/
"""
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.db.backends import utils as backend_utils
from django.db.backends.base.base import BaseDatabaseWrapper
from django.utils.asyncio import async_unsafe
from django.utils.functional import cached_property
from django.utils.regex_helper import _lazy_re_compile

try:
    import MySQLdb as Database
except ImportError as err:
    raise ImproperlyConfigured(
        "Error loading MySQLdb module.\nDid you install mysqlclient?"
    ) from err

from MySQLdb.constants import CLIENT, FIELD_TYPE
from MySQLdb.converters import conversions

# Some of these import MySQLdb, so import them after checking if it's installed.
from .client import DatabaseClient
from .creation import DatabaseCreation
from .features import DatabaseFeatures
from .introspection import DatabaseIntrospection
from .operations import DatabaseOperations
from .schema import DatabaseSchemaEditor
from .validation import DatabaseValidation

version = Database.version_info
if version < (1, 4, 3):
    raise ImproperlyConfigured(
        "mysqlclient 1.4.3 or newer is required; you have %s." % Database.__version__
    )

# MySQLdb returns TIME columns as timedelta -- they are more like timedelta in
# terms of actual behavior as they are signed and include days -- and Django
# expects time.
django_conversions = {
    **conversions,
    **{FIELD_TYPE.TIME: backend_utils.typecast_time},
}

# This should match the numerical portion of the version numbers (we can treat
# versions like 5.0.24 and 5.0.24a as the same).
server_version_re = _lazy_re_compile(r"(\d{1,2})\.(\d{1,2})\.(\d{1,2})")


class CursorWrapper:
    """
    A thin wrapper around MySQLdb's normal cursor class that catches particular
    exception instances and reraises them with the correct types.

    Implemented as a wrapper, rather than a subclass, so that it isn't stuck
    to the particular underlying representation returned by Connection.cursor().
    """

    codes_for_integrityerror = (
        1048,  # Column cannot be null
        1690,  # BIGINT UNSIGNED value is out of range
        3819,  # CHECK constraint is violated
        4025,  # CHECK constraint failed
    )

    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, args=None):
        try:
            # args is None means no string interpolation
            return self.cursor.execute(query, args)
        except Database.OperationalError as e:
            # Map some error codes to IntegrityError, since they seem to be
            # misclassified and Django would prefer the more logical place.
            if e.args[0] in self.codes_for_integrityerror:
                raise IntegrityError(*tuple(e.args))
            raise

    def executemany(self, query, args):
        try:
            return self.cursor.executemany(query, args)
        except Database.OperationalError as e:
            # Map some error codes to IntegrityError, since they seem to be
            # misclassified and Django would prefer the more logical place.
            if e.args[0] in self.codes_for_integrityerror:
                raise IntegrityError(*tuple(e.args))
            raise

    def __getattr__(self, attr):
        return getattr(self.cursor, attr)

    def __iter__(self):
        # self.cursor -> MySQLdb.cursors.BaseCursor
        # MySQLdb.cursors.BaseCursor 中重写了__iter__方法，返回了一个“fetchone”的查询结果
        return iter(self.cursor)


class DatabaseWrapper(BaseDatabaseWrapper):
    # 定义数据库供应商为MySQL
    vendor = "mysql"
    # 这个字典将Field对象映射到它们关联的MySQL列类型，作为字符串
    # 列类型字符串可以包含格式字符串；它们将在被输出之前针对Field.__dict__的值进行插值
    # 如果列类型被设置为None，它将不会被包含在输出中
    data_types = {
        "AutoField": "integer AUTO_INCREMENT",
        "BigAutoField": "bigint AUTO_INCREMENT",
        "BinaryField": "longblob",
        "BooleanField": "bool",
        "CharField": "varchar(%(max_length)s)",
        "DateField": "date",
        "DateTimeField": "datetime(6)",
        "DecimalField": "numeric(%(max_digits)s, %(decimal_places)s)",
        "DurationField": "bigint",
        "FileField": "varchar(%(max_length)s)",
        "FilePathField": "varchar(%(max_length)s)",
        "FloatField": "double precision",
        "IntegerField": "integer",
        "BigIntegerField": "bigint",
        "IPAddressField": "char(15)",
        "GenericIPAddressField": "char(39)",
        "JSONField": "json",
        "OneToOneField": "integer",
        "PositiveBigIntegerField": "bigint UNSIGNED",
        "PositiveIntegerField": "integer UNSIGNED",
        "PositiveSmallIntegerField": "smallint UNSIGNED",
        "SlugField": "varchar(%(max_length)s)",
        "SmallAutoField": "smallint AUTO_INCREMENT",
        "SmallIntegerField": "smallint",
        "TextField": "longtext",
        "TimeField": "time(6)",
        "UUIDField": "char(32)",
    }

    # 对于这些数据类型：
    # - MySQL < 8.0.13不接受默认值，并隐式地将它们视为可空
    # - 所有版本的MySQL和MariaDB不支持全宽度数据库索引
    _limited_data_types = (
        "tinyblob",
        "blob",
        "mediumblob",
        "longblob",
        "tinytext",
        "text",
        "mediumtext",
        "longtext",
        "json",
    )

    # 定义SQL查询操作符
    operators = {
        "exact": "= %s",
        "iexact": "LIKE %s",
        "contains": "LIKE BINARY %s",
        "icontains": "LIKE %s",
        "gt": "> %s",
        "gte": ">= %s",
        "lt": "< %s",
        "lte": "<= %s",
        "startswith": "LIKE BINARY %s",
        "endswith": "LIKE BINARY %s",
        "istartswith": "LIKE %s",
        "iendswith": "LIKE %s",
    }

    # 用于生成SQL模式查找子句的模式
    # 当查找的右-hand side不是原始字符串时（可能是表达式或双边转换的结果），应使用这些模式
    # 在这些情况下，LIKE操作符的特殊字符（例如\, *, _）应该在数据库侧进行转义
    #
    # 注意：我们在这里使用str.format()来提高可读性，因为'%'是LIKE操作符的通配符
    pattern_esc = r"REPLACE(REPLACE(REPLACE({}, '\\', '\\\\'), '%%', '\%%'), '_', '\_')"
    pattern_ops = {
        "contains": "LIKE BINARY CONCAT('%%', {}, '%%')",
        "icontains": "LIKE CONCAT('%%', {}, '%%')",
        "startswith": "LIKE BINARY CONCAT({}, '%%')",
        "istartswith": "LIKE CONCAT({}, '%%')",
        "endswith": "LIKE BINARY CONCAT('%%', {})",
        "iendswith": "LIKE CONCAT('%%', {})",
    }

    # 定义事务隔离级别
    isolation_levels = {
        "read uncommitted",
        "read committed",
        "repeatable read",
        "serializable",
    }

    # 关联数据库类
    Database = Database
    # 关联模式编辑器类
    SchemaEditorClass = DatabaseSchemaEditor
    # 在__init__()中实例化的类
    client_class = DatabaseClient
    creation_class = DatabaseCreation
    features_class = DatabaseFeatures
    introspection_class = DatabaseIntrospection
    ops_class = DatabaseOperations
    validation_class = DatabaseValidation

    def get_database_version(self):
        return self.mysql_version

    def get_connection_params(self):
        kwargs = {
            "conv": django_conversions,
            "charset": "utf8",
        }
        settings_dict = self.settings_dict
        if settings_dict["USER"]:
            kwargs["user"] = settings_dict["USER"]
        if settings_dict["NAME"]:
            kwargs["database"] = settings_dict["NAME"]
        if settings_dict["PASSWORD"]:
            kwargs["password"] = settings_dict["PASSWORD"]
        if settings_dict["HOST"].startswith("/"):
            kwargs["unix_socket"] = settings_dict["HOST"]
        elif settings_dict["HOST"]:
            kwargs["host"] = settings_dict["HOST"]
        if settings_dict["PORT"]:
            kwargs["port"] = int(settings_dict["PORT"])
        # We need the number of potentially affected rows after an
        # "UPDATE", not the number of changed rows.
        kwargs["client_flag"] = CLIENT.FOUND_ROWS
        # Validate the transaction isolation level, if specified.
        options = settings_dict["OPTIONS"].copy()
        isolation_level = options.pop("isolation_level", "read committed")
        if isolation_level:
            isolation_level = isolation_level.lower()
            if isolation_level not in self.isolation_levels:
                raise ImproperlyConfigured(
                    "Invalid transaction isolation level '%s' specified.\n"
                    "Use one of %s, or None."
                    % (
                        isolation_level,
                        ", ".join("'%s'" % s for s in sorted(self.isolation_levels)),
                    )
                )
        self.isolation_level = isolation_level
        kwargs.update(options)
        return kwargs

    @async_unsafe
    def get_new_connection(self, conn_params):
        connection = Database.connect(**conn_params)
        # bytes encoder in mysqlclient doesn't work and was added only to
        # prevent KeyErrors in Django < 2.0. We can remove this workaround when
        # mysqlclient 2.1 becomes the minimal mysqlclient supported by Django.
        # See https://github.com/PyMySQL/mysqlclient/issues/489
        if connection.encoders.get(bytes) is bytes:
            connection.encoders.pop(bytes)
        return connection

    def init_connection_state(self):
        super().init_connection_state()
        assignments = []
        if self.features.is_sql_auto_is_null_enabled:
            # SQL_AUTO_IS_NULL controls whether an AUTO_INCREMENT column on
            # a recently inserted row will return when the field is tested
            # for NULL. Disabling this brings this aspect of MySQL in line
            # with SQL standards.
            assignments.append("SET SQL_AUTO_IS_NULL = 0")

        if self.isolation_level:
            assignments.append(
                "SET SESSION TRANSACTION ISOLATION LEVEL %s"
                % self.isolation_level.upper()
            )

        if assignments:
            with self.cursor() as cursor:
                cursor.execute("; ".join(assignments))

    @async_unsafe
    def create_cursor(self, name=None):
        cursor = self.connection.cursor()
        return CursorWrapper(cursor)

    def _rollback(self):
        try:
            BaseDatabaseWrapper._rollback(self)
        except Database.NotSupportedError:
            pass

    def _set_autocommit(self, autocommit):
        with self.wrap_database_errors:
            self.connection.autocommit(autocommit)

    def disable_constraint_checking(self):
        """
        Disable foreign key checks, primarily for use in adding rows with
        forward references. Always return True to indicate constraint checks
        need to be re-enabled.
        """
        with self.cursor() as cursor:
            cursor.execute("SET foreign_key_checks=0")
        return True

    def enable_constraint_checking(self):
        """
        Re-enable foreign key checks after they have been disabled.
        """
        # Override needs_rollback in case constraint_checks_disabled is
        # nested inside transaction.atomic.
        self.needs_rollback, needs_rollback = False, self.needs_rollback
        try:
            with self.cursor() as cursor:
                cursor.execute("SET foreign_key_checks=1")
        finally:
            self.needs_rollback = needs_rollback

    def check_constraints(self, table_names=None):
        """
        Check each table name in `table_names` for rows with invalid foreign
        key references. This method is intended to be used in conjunction with
        `disable_constraint_checking()` and `enable_constraint_checking()`, to
        determine if rows with invalid references were entered while constraint
        checks were off.
        """
        with self.cursor() as cursor:
            if table_names is None:
                table_names = self.introspection.table_names(cursor)
            for table_name in table_names:
                primary_key_column_name = self.introspection.get_primary_key_column(
                    cursor, table_name
                )
                if not primary_key_column_name:
                    continue
                relations = self.introspection.get_relations(cursor, table_name)
                for column_name, (
                        referenced_column_name,
                        referenced_table_name,
                ) in relations.items():
                    cursor.execute(
                        """
                        SELECT REFERRING.`%s`, REFERRING.`%s` FROM `%s` as REFERRING
                        LEFT JOIN `%s` as REFERRED
                        ON (REFERRING.`%s` = REFERRED.`%s`)
                        WHERE REFERRING.`%s` IS NOT NULL AND REFERRED.`%s` IS NULL
                        """
                        % (
                            primary_key_column_name,
                            column_name,
                            table_name,
                            referenced_table_name,
                            column_name,
                            referenced_column_name,
                            column_name,
                            referenced_column_name,
                        )
                    )
                    for bad_row in cursor.fetchall():
                        raise IntegrityError(
                            "The row in table '%s' with primary key '%s' has an "
                            "invalid foreign key: %s.%s contains a value '%s' that "
                            "does not have a corresponding value in %s.%s."
                            % (
                                table_name,
                                bad_row[0],
                                table_name,
                                column_name,
                                bad_row[1],
                                referenced_table_name,
                                referenced_column_name,
                            )
                        )

    def is_usable(self):
        try:
            self.connection.ping()
        except Database.Error:
            return False
        else:
            return True

    @cached_property
    def display_name(self):
        return "MariaDB" if self.mysql_is_mariadb else "MySQL"

    @cached_property
    def data_type_check_constraints(self):
        if self.features.supports_column_check_constraints:
            check_constraints = {
                "PositiveBigIntegerField": "`%(column)s` >= 0",
                "PositiveIntegerField": "`%(column)s` >= 0",
                "PositiveSmallIntegerField": "`%(column)s` >= 0",
            }
            if self.mysql_is_mariadb and self.mysql_version < (10, 4, 3):
                # MariaDB < 10.4.3 doesn't automatically use the JSON_VALID as
                # a check constraint.
                check_constraints["JSONField"] = "JSON_VALID(`%(column)s`)"
            return check_constraints
        return {}

    @cached_property
    def mysql_server_data(self):
        with self.temporary_connection() as cursor:
            # Select some server variables and test if the time zone
            # definitions are installed. CONVERT_TZ returns NULL if 'UTC'
            # timezone isn't loaded into the mysql.time_zone table.
            cursor.execute(
                """
                SELECT VERSION(),
                       @@sql_mode,
                       @@default_storage_engine,
                       @@sql_auto_is_null,
                       @@lower_case_table_names,
                       CONVERT_TZ('2001-01-01 01:00:00', 'UTC', 'UTC') IS NOT NULL
            """
            )
            row = cursor.fetchone()
        return {
            "version": row[0],
            "sql_mode": row[1],
            "default_storage_engine": row[2],
            "sql_auto_is_null": bool(row[3]),
            "lower_case_table_names": bool(row[4]),
            "has_zoneinfo_database": bool(row[5]),
        }

    @cached_property
    def mysql_server_info(self):
        return self.mysql_server_data["version"]

    @cached_property
    def mysql_version(self):
        match = server_version_re.match(self.mysql_server_info)
        if not match:
            raise Exception(
                "Unable to determine MySQL version from version string %r"
                % self.mysql_server_info
            )
        return tuple(int(x) for x in match.groups())

    @cached_property
    def mysql_is_mariadb(self):
        return "mariadb" in self.mysql_server_info.lower()

    @cached_property
    def sql_mode(self):
        sql_mode = self.mysql_server_data["sql_mode"]
        return set(sql_mode.split(",") if sql_mode else ())
