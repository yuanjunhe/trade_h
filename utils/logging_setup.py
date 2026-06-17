"""
统一的日志配置。
"""

import logging
import sys


def setup_logging(level: str = "INFO") -> logging.Logger:
    """配置根日志器。

    参数:
        level: 日志级别（DEBUG, INFO, WARNING, ERROR）。

    返回:
        根日志器。
    """
    logger = logging.getLogger("quant")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    return logger


def get_logger(name: str = "quant") -> logging.Logger:
    """获取子日志器。"""
    return logging.getLogger(f"quant.{name}")
