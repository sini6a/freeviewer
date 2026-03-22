import pymysql
import pymysql.cursors
from flask import g, current_app


def get_db():
    if "db" not in g:
        g.db = pymysql.connect(
            host=current_app.config["MYSQL_HOST"],
            port=current_app.config["MYSQL_PORT"],
            user=current_app.config["MYSQL_USER"],
            password=current_app.config["MYSQL_PASSWORD"],
            database=current_app.config["MYSQL_DATABASE"],
            cursorclass=pymysql.cursors.DictCursor,
            charset="utf8mb4",
            autocommit=False,
        )
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_one(query, params=()):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(query, params or ())
        return cur.fetchone()


def query_all(query, params=()):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(query, params or ())
        return cur.fetchall()


def execute(query, params=()):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(query, params or ())
    db.commit()


def ensure_column(table_name, column_name, column_definition):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table_name, column_name),
        )
        if cur.fetchone() is None:
            cur.execute(
                f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_definition}"
            )
    db.commit()


def init_db():
    db = get_db()
    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(150) NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'user',
            banned TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INT AUTO_INCREMENT PRIMARY KEY,
            display_name TEXT NOT NULL,
            device_code VARCHAR(100) NOT NULL UNIQUE,
            owner_email VARCHAR(255),
            owner_user_id INT,
            status VARCHAR(20) NOT NULL DEFAULT 'offline',
            pairing_state VARCHAR(20) NOT NULL DEFAULT 'pending',
            hostname TEXT,
            os_name TEXT,
            os_version TEXT,
            last_seen_at TEXT,
            device_token TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_user_id) REFERENCES users (id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pairing_requests (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            code VARCHAR(20) NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            expires_at TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS settings (
            `key` VARCHAR(100) PRIMARY KEY,
            value TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            username VARCHAR(150) PRIMARY KEY,
            failed_count INT NOT NULL DEFAULT 0,
            locked_until TEXT,
            lockout_level INT NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS login_attempt_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(150) NOT NULL,
            ip_address VARCHAR(45),
            attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]
    with db.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
    db.commit()

    # Column migrations
    ensure_column("devices", "owner_user_id", "INT")
    ensure_column("devices", "hostname", "TEXT")
    ensure_column("devices", "os_name", "TEXT")
    ensure_column("devices", "os_version", "TEXT")
    ensure_column("devices", "device_token", "TEXT")
    ensure_column("users", "role", "VARCHAR(20) NOT NULL DEFAULT 'user'")
    ensure_column("users", "banned", "TINYINT(1) NOT NULL DEFAULT 0")

    # Migrate username column if missing
    db2 = get_db()
    with db2.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'username'"
        )
        has_username = cur.fetchone() is not None
        if not has_username:
            cur.execute(
                "ALTER TABLE users ADD COLUMN username VARCHAR(150) NOT NULL DEFAULT ''"
            )
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'email'"
            )
            if cur.fetchone() is not None:
                cur.execute("UPDATE users SET username = email WHERE username = ''")
    db2.commit()
