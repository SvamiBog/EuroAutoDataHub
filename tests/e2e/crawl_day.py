"""Один «день» обхода: настоящий паук и KafkaPipeline против фейкового сайта.

Запускается отдельным процессом (reactor Twisted нельзя перезапустить): python crawl_day.py <сценарий.json>
Сообщения уходят в Kafka с источником e2e.test, чтобы не смешиваться с настоящими данными.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/scrapy_spiders/car_scrapers"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(ROOT / "services/scrapy_spiders/car_scrapers")  # scrapy.cfg проекта

from fake_site import serve  # noqa: E402
from scrapy.crawler import CrawlerProcess  # noqa: E402
from scrapy.utils.project import get_project_settings  # noqa: E402

from car_scrapers.spiders.otomoto import OtomotoSpider  # noqa: E402

SOURCE = "e2e.test"

with open(sys.argv[1]) as f:
    scenario = json.load(f)
server = serve(scenario["catalog"], scenario.get("blocked", []), scenario.get("broken"))


class E2ESpider(OtomotoSpider):
    name = "e2e"
    SOURCE_NAME = SOURCE
    BASE_URL = f"http://127.0.0.1:{server.server_port}/graphql"
    allowed_domains = ["127.0.0.1"]


settings = get_project_settings()
settings.setdict({
    "KAFKA_BOOTSTRAP_SERVERS": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094"),
    "PAUSE_DURATION": 1, "MAX_PAUSES": 3, "LOG_FILE": None, "LOG_LEVEL": "WARNING",
    "PROGRESS_BAR": "false", "AUTOTHROTTLE_ENABLED": False, "DOWNLOAD_DELAY": 0,
    "RAW_RESPONSES_DIR": "", **scenario.get("settings", {}),
}, priority="cmdline")
process = CrawlerProcess(settings)
crawler = process.create_crawler(E2ESpider)
process.crawl(crawler, makes=",".join(scenario["catalog"]))
process.start()
print("RESULT " + json.dumps({"run_id": crawler.spider.run_id,
                              "shards": {k: v["complete"] for k, v in crawler.spider.make_results.items()}}))
