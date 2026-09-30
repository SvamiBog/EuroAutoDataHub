# services/scrapy_spiders/car_scrapers/car_scrapers/pipelines.py
"""Отправка сообщений в Kafka по контракту libs/eadh_common/messages.py (схема v1).

- ListingObservationItem -> KAFKA_TOPIC_OBSERVATIONS, ключ "source:source_listing_id";
- CrawlEventItem (shard_finished) -> KAFKA_TOPIC_CRAWL_EVENTS, ключ run_id;
- run_started отправляется при открытии паука, run_finished — по сигналу spider_closed
  (там известна причина завершения).
"""
import json
import logging
from datetime import datetime, timezone

from itemadapter import ItemAdapter
from kafka import KafkaProducer
from kafka.errors import KafkaError
from scrapy import signals

from .items import CrawlEventItem, ListingObservationItem

SCHEMA_VERSION = 1


class KafkaPipeline:
    def __init__(self, kafka_bootstrap_servers, kafka_topic_observations, kafka_topic_crawl_events,
                 kafka_producer_config=None, stats=None):
        self.kafka_bootstrap_servers = kafka_bootstrap_servers
        self.kafka_topic_observations = kafka_topic_observations
        self.kafka_topic_crawl_events = kafka_topic_crawl_events
        self.producer = None
        self.kafka_producer_config = kafka_producer_config if kafka_producer_config else {}
        self.stats = stats
        self.logger = logging.getLogger(self.__class__.__name__)

    @classmethod
    def from_crawler(cls, crawler):
        # Получаем настройки из settings.py Scrapy
        kafka_bootstrap_servers = crawler.settings.getlist('KAFKA_BOOTSTRAP_SERVERS')
        kafka_topic_observations = crawler.settings.get('KAFKA_TOPIC_OBSERVATIONS')
        kafka_topic_crawl_events = crawler.settings.get('KAFKA_TOPIC_CRAWL_EVENTS')

        if not kafka_bootstrap_servers or not kafka_topic_observations or not kafka_topic_crawl_events:
            raise ValueError("Один из обязательных параметров Kafka (BOOTSTRAP_SERVERS, TOPIC_OBSERVATIONS, "
                             "TOPIC_CRAWL_EVENTS) не настроен в settings.py")

        pipeline = cls(
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_topic_observations=kafka_topic_observations,
            kafka_topic_crawl_events=kafka_topic_crawl_events,
            kafka_producer_config=crawler.settings.getdict('KAFKA_PRODUCER_CONFIG', {}),
            stats=crawler.stats,
        )
        crawler.signals.connect(pipeline.spider_closed, signal=signals.spider_closed)
        return pipeline

    def _create_producer(self):
        return KafkaProducer(
            bootstrap_servers=self.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v, default=str).encode('utf-8'),
            **self.kafka_producer_config
        )

    def open_spider(self, spider):
        try:
            self.producer = self._create_producer()
        except KafkaError as e:
            # Без Kafka обход бессмыслен: все собранные данные были бы потеряны
            self.logger.critical(f"Не удалось подключиться к Kafka {self.kafka_bootstrap_servers}: {e}")
            raise
        self.logger.info(f"KafkaProducer подключен к {self.kafka_bootstrap_servers}")

        self._send_crawl_event(spider, {
            'event': 'run_started',
            'started_at': spider.run_started_at.isoformat(),
            'shards_planned': spider.shards_planned,
        })

    def close_spider(self, spider):
        # Продюсер закрывается в spider_closed: после него еще уходит run_finished
        if self.producer:
            self.producer.flush()

    def spider_closed(self, spider, reason):
        if not self.producer:
            return
        try:
            self._send_crawl_event(spider, {
                'event': 'run_finished',
                'finished_at': datetime.now(timezone.utc).isoformat(),
                'finish_reason': reason,
                'stats': spider.run_summary(),
            })
            self.producer.flush()
            self.producer.close()
        except Exception as e:
            self.logger.error(f"KafkaPipeline: ошибка при завершении работы с Kafka: {e}", exc_info=True)
        finally:
            self.producer = None

        failed = self.stats.get_value('kafka/send_failed', 0) if self.stats else 0
        if failed:
            self.logger.error(f"KafkaPipeline: {failed} сообщений не доставлено в Kafka")

    def process_item(self, item, spider):
        if not self.producer:
            raise RuntimeError("KafkaProducer не инициализирован")

        item_dict = ItemAdapter(item).asdict()

        if isinstance(item, ListingObservationItem):
            self._send(self.kafka_topic_observations,
                       f"{item_dict.get('source')}:{item_dict.get('source_listing_id')}",
                       {'schema_version': SCHEMA_VERSION, **item_dict})
        elif isinstance(item, CrawlEventItem):
            self._send(self.kafka_topic_crawl_events, item_dict['run_id'],
                       {'schema_version': SCHEMA_VERSION, **item_dict})
            self.logger.info(f"Шард {item_dict.get('shard_key')} завершен: "
                             f"{item_dict.get('collected_count')}/{item_dict.get('expected_count')}, "
                             f"полный: {item_dict.get('complete')}")
        else:
            # Если появится какой-то другой тип item, просто его пропустим
            self.logger.warning(f"Неизвестный тип item: {type(item)}. Элемент не будет отправлен в Kafka.")
        return item

    def _send_crawl_event(self, spider, payload):
        message = {'schema_version': SCHEMA_VERSION, 'run_id': spider.run_id,
                   'source': spider.SOURCE_NAME, **payload}
        self._send(self.kafka_topic_crawl_events, spider.run_id, message)

    def _send(self, topic, key, value):
        future = self.producer.send(topic, key=key.encode('utf-8') if key else None, value=value)
        future.add_callback(self._on_send_success, topic)
        future.add_errback(self._on_send_error, topic, key)

    def _on_send_success(self, topic, record_metadata=None):
        if self.stats:
            self.stats.inc_value(f'kafka/sent/{topic}')

    def _on_send_error(self, topic, message_key, exc):
        if self.stats:
            self.stats.inc_value('kafka/send_failed')
        self.logger.error(f"Ошибка доставки в Kafka (топик {topic}, ключ {message_key}): {exc}")
