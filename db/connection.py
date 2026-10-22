import pymysql
from core.settings import get_settings


_settings = get_settings()
MYSQL_HOST = _settings.mysql_host
MYSQL_PORT = _settings.mysql_port
MYSQL_USER = _settings.mysql_user
MYSQL_PASSWORD = _settings.mysql_password
MYSQL_DATABASE = _settings.mysql_database


def get_server_connection():
    """
    连接 MySQL 服务本身。

    注意：
    这里暂时不指定 database。

    主要用于：
    CREATE DATABASE
    """

    connection = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        connect_timeout=_settings.mysql_connect_timeout,
        read_timeout=2,
        write_timeout=2,
        init_command="SET SESSION innodb_lock_wait_timeout=1"
    )

    return connection


def get_connection():
    """
    连接当前项目数据库。

    后面所有正常的增删改查，
    基本都会使用这个函数。
    """

    connection = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=_settings.mysql_connect_timeout,
        read_timeout=2,
        write_timeout=2,
        init_command="SET SESSION innodb_lock_wait_timeout=1"
    )

    return connection
