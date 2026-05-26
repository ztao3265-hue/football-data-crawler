"""PostgreSQL 连接管理"""

import os
from urllib.parse import quote_plus
from contextlib import contextmanager

from sqlalchemy import create_engine, Engine, text
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

from crawler.core.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/football_crawler"


def get_database_url() -> str:
    """从环境变量获取 PostgreSQL 连接字符串，也支持逐个字段拼接"""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "football_crawler")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")

    encoded_password = quote_plus(password)
    return f"postgresql://{user}:{encoded_password}@{host}:{port}/{name}"


class DatabaseManager:
    """PostgreSQL 数据库管理器"""

    def __init__(self, database_url: str = None):
        self.database_url = database_url or get_database_url()
        self._engine: Engine | None = None
        self._session_factory: sessionmaker | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self.database_url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
                echo=False,
            )
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.engine)
        return self._session_factory

    @contextmanager
    def session(self) -> Session:
        """获取会话上下文"""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def test_connection(self) -> bool:
        """测试数据库连接"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("SELECT version()"))
                version = result.scalar()
                logger.info(f"PostgreSQL 连接成功: {version[:50]}...")
                return True
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            logger.info("请确认 PostgreSQL 已启动，且 DATABASE_URL 或 DB_* 环境变量配置正确")
            return False

    def create_all(self):
        """创建所有表"""
        from crawler.database.schema import Base
        Base.metadata.create_all(self.engine)
        logger.info("数据库表创建/更新完成")

    def drop_all(self):
        """删除所有表（危险操作）"""
        from crawler.database.schema import Base
        Base.metadata.drop_all(self.engine)
        logger.warning("所有数据库表已删除")


# 全局实例
_db_manager: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
