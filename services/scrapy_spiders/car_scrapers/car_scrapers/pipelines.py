# services/scrapy_spiders/car_scrapers/car_scrapers/pipelines.py
import json
import logging
from itemadapter import ItemAdapter
from kafka import KafkaProducer
from kafka.errors import KafkaError
from .items import ParsedAdItem, ActiveIdsItem


class KafkaPipeline:
    def __init__(self, kafka_bootstrap_servers, kafka_topic_ads, kafka_topic_active_ids,
                 kafka_producer_config=None, stats=None):
        self.kafka_bootstrap_servers = kafka_bootstrap_servers
        self.kafka_topic_ads = kafka_topic_ads
        self.kafka_topic_active_ids = kafka_topic_active_ids
        self.producer = None
        self.kafka_producer_config = kafka_producer_config if kafka_producer_config else {}
        self.stats = stats
        self.logger = logging.getLogger(self.__class__.__name__)

    @classmethod
    def from_crawler(cls, crawler):
        # Получаем настройки из settings.py Scrapy
        kafka_bootstrap_servers = crawler.settings.getlist('KAFKA_BOOTSTRAP_SERVERS')
        kafka_topic_ads = crawler.settings.get('KAFKA_TOPIC_ADS')
        kafka_topic_active_ids = crawler.settings.get('KAFKA_TOPIC_ACTIVE_IDS')

        kafka_producer_config = crawler.settings.getdict('KAFKA_PRODUCER_CONFIG', {})

        if not kafka_bootstrap_servers or not kafka_topic_ads or not kafka_topic_active_ids:
            raise ValueError(
                "Один из обязательных параметров Kafka (BOOTSTRAP_SERVERS, TOPIC_ADS, TOPIC_ACTIVE_IDS) не настроен в settings.py")

        return cls(
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_topic_ads=kafka_topic_ads,
            kafka_topic_active_ids=kafka_topic_active_ids,
            kafka_producer_config=kafka_producer_config,
            stats=crawler.stats,
        )

    def open_spider(self, spider):
        try:
            self.producer = self._create_producer()
        except KafkaError as e:
            # Без Kafka обход бессмыслен: все собранные данные были бы потеряны
            self.logger.critical(f"Не удалось подключиться к Kafka {self.kafka_bootstrap_servers}: {e}")
            raise
        self.logger.info(f"KafkaProducer подключен к {self.kafka_bootstrap_servers}")

    def _create_producer(self):
        return KafkaProducer(
            bootstrap_servers=self.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            **self.kafka_producer_config
        )

    def close_spider(self, spider):
        if not self.producer:
            return
        try:
            self.logger.info(f"KafkaPipeline: Flushing and closing KafkaProducer для паука {spider.name}.")
            self.producer.flush()
            self.producer.close()
        except Exception as e:
            self.logger.error(f"KafkaPipeline: Ошибка при закрытии KafkaProducer для паука {spider.name}: {e}", exc_info=True)

        failed = self.stats.get_value('kafka/send_failed', 0) if self.stats else 0
        if failed:
            self.logger.error(f"KafkaPipeline: {failed} сообщений не доставлено в Kafka")

    def process_item(self, item, spider):
        if not self.producer:
            raise RuntimeError("KafkaProducer не инициализирован")

        item_dict = ItemAdapter(item).asdict()

        if isinstance(item, ParsedAdItem):
            topic = self.kafka_topic_ads
            message_key = item_dict.get('source_ad_id') or item_dict.get('url')
        elif isinstance(item, ActiveIdsItem):
            topic = self.kafka_topic_active_ids
            # Ключ — источник и марка: сообщения одной марки попадают в одну партицию по порядку
            message_key = f"{item_dict.get('source_name')}:{item_dict.get('make_str')}"
        else:
            # Если появится какой-то другой тип item, просто его пропустим
            self.logger.warning(f"Неизвестный тип item: {type(item)}. Элемент не будет отправлен в Kafka.")
            return item

        future = self.producer.send(topic, key=message_key.encode('utf-8') if message_key else None, value=item_dict)
        future.add_callback(self._on_send_success, topic)
        future.add_errback(self._on_send_error, topic, message_key)

        if isinstance(item, ActiveIdsItem):
            self.logger.info(f"Список из {len(item_dict.get('ad_ids', []))} активных ID марки "
                             f"{item_dict.get('make_str')} отправлен в топик {topic}")
        return item

    def _on_send_success(self, topic, record_metadata=None):
        if self.stats:
            self.stats.inc_value(f'kafka/sent/{topic}')

    def _on_send_error(self, topic, message_key, exc):
        if self.stats:
            self.stats.inc_value('kafka/send_failed')
        self.logger.error(f"Ошибка доставки в Kafka (топик {topic}, ключ {message_key}): {exc}")
