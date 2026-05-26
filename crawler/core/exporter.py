"""数据导出 - JSON / CSV / Excel"""

import json
from pathlib import Path
from datetime import datetime
from typing import List

import pandas as pd

from crawler.core.logger import get_logger

logger = get_logger(__name__)


class Exporter:
    """数据导出器"""

    def __init__(self, output_dir: str = "exports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, matches: list, date_str: str = None,
               filename_prefix: str = "matches"):
        """导出为 JSON 和 CSV"""
        if not matches:
            logger.warning("没有数据可导出")
            return

        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        data = [m.to_dict() if hasattr(m, "to_dict") else m for m in matches]

        # JSON
        json_path = self.output_dir / f"{filename_prefix}_{date_str}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"JSON 已导出: {json_path} ({len(data)} 条记录)")

        # CSV
        csv_path = self.output_dir / f"{filename_prefix}_{date_str}.csv"
        df = pd.DataFrame(data)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"CSV 已导出: {csv_path} ({len(data)} 条记录)")

        # Today 快捷链接
        today_json = self.output_dir / "matches_today.json"
        today_csv = self.output_dir / "matches_today.csv"
        with open(today_json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        pd.DataFrame(data).to_csv(today_csv, index=False, encoding="utf-8-sig")
        logger.info("已更新 matches_today.json 和 matches_today.csv")

    def export_summary(self, results: list, date_str: str = None):
        """导出采集摘要"""
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        summary = {
            "date": date_str,
            "exported_at": datetime.now().isoformat(),
            "total_sources": len(results),
            "sources": [r.to_dict() if hasattr(r, "to_dict") else r for r in results],
        }

        path = self.output_dir / f"summary_{date_str}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info(f"摘要已导出: {path}")
